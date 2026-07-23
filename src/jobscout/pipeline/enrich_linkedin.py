"""Backfill LinkedIn offer descriptions from the public guest endpoint.

LinkedIn alert *emails* give us title/company/location/URL but no description
and no contract — so those offers sit at ``needs_review`` and, more importantly,
the LLM scorer has nothing to reason about. This stage fetches each such offer's
public **guest** job-posting page (no login — the endpoint LinkedIn itself
serves to logged-out visitors, the "public guest endpoint, low volume" path the
brief permits) and backfills the description, then re-runs the hard filters on
exactly the rows it changed.

Why it's a standalone post-ingest stage (not part of the adapter): the fetch is
rate-limited, so it must be *cached* and *idempotent* — it should touch only
offers still missing a description and never re-hit one it already fetched.
That requires post-upsert DB state (which rows are new, which are done), which
an adapter — a pure producer with no DB access — can't see. Placing it here
keeps ingest fast and deterministic and lets caching/rate-limiting/fail-soft
live where they work.

Rate-limit discipline (LinkedIn soft-blocks abuse of the guest endpoint):
  * **sequential**, with a **jittered delay** between fetches;
  * a **per-run cap** (``--limit``);
  * a **raw-response cache** under ``data/linkedin_guest/`` — a cached job is
    read from disk, never re-fetched;
  * **fail-soft**: a non-200, a block, or an unparseable page leaves the row
    untouched (``description`` stays NULL → still ``needs_review``) and the run
    continues. A wrong/blocked fetch can never corrupt a record.

Nothing here logs or stores anything beyond the public posting content.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jobscout import config, normalize
from jobscout.adapters.linkedin_guest import parse_job_posting
from jobscout.models import JobRecord
from jobscout.pipeline import ProgressFn
from jobscout.pipeline.filters import Outcome, apply_hard_filters
from jobscout.storage import db

logger = logging.getLogger("jobscout.enrich_linkedin")

GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
DEFAULT_CACHE_DIR = Path("data/linkedin_guest")

# A plain browser UA + language; the guest endpoint serves logged-out clients.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}


@dataclass
class EnrichSummary:
    """The run's tally."""

    dry_run: bool = False
    considered: int = 0   # rows that needed a description
    enriched: int = 0     # rows we successfully backfilled
    from_cache: int = 0   # of those, served from the on-disk cache
    failed: int = 0       # fetches that returned nothing usable (fail-soft)
    refiltered: int = 0   # enriched rows whose verdict we re-computed
    refilter_status_changes: dict[str, int] = field(default_factory=dict)


def run_enrich_linkedin(
    *,
    prefs_path=config.DEFAULT_PREFERENCES_PATH,
    db_path=db.DEFAULT_DB_PATH,
    limit: int | None = 25,
    min_delay: float = 2.0,
    max_delay: float = 5.0,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    dry_run: bool = False,
    on_progress: ProgressFn | None = None,
    _client: httpx.Client | None = None,
) -> EnrichSummary:
    """Fetch guest pages for LinkedIn offers missing a description; backfill.

    Processes at most ``limit`` offers that still lack a description, politely
    (sequential + jittered ``min_delay``..``max_delay`` between *network*
    fetches; cache hits incur no delay). On success it backfills the description
    and re-runs the hard filters on that row so its verdict reflects the new
    prose. ``dry_run`` fetches/parses and reports but writes nothing.

    ``_client`` is injectable so tests can supply a transport that never touches
    the network.
    """
    prefs = config.load_preferences(prefs_path)
    summary = EnrichSummary(dry_run=dry_run)
    cache_dir.mkdir(parents=True, exist_ok=True)

    conn = db.connect(db_path)
    run_id = None if dry_run else db.record_run_start(conn, dry_run=dry_run)
    owns_client = _client is None
    client = _client or httpx.Client(timeout=20.0, headers=_HEADERS, follow_redirects=True)

    try:
        rows = db.select_jobs_missing_description(conn, limit=limit)
        summary.considered = len(rows)
        made_network_call = False

        for i, row in enumerate(rows, 1):
            job_id = row["external_id"]
            html, was_cached = _get_html(
                client, job_id, cache_dir, dry_run=dry_run,
                delay=(min_delay, max_delay),
                skip_delay_before_first=not made_network_call,
            )
            if not was_cached and html is not None:
                made_network_call = True

            posting = parse_job_posting(html)
            if not posting.description:
                summary.failed += 1
                logger.info("no description for LinkedIn %s (left needs_review)", job_id)
            else:
                summary.enriched += 1
                if was_cached:
                    summary.from_cache += 1

                contract = _resolve_contract(posting.employment_type)
                if not dry_run:
                    db.backfill_description(
                        conn, row["id"],
                        description=posting.description,
                        contract_type=contract,
                    )
                    _refilter_row(conn, row["id"], prefs, summary)

            if on_progress is not None:
                on_progress(i, summary.considered)

        if not dry_run:
            conn.commit()
    finally:
        if owns_client:
            client.close()
        if run_id is not None:
            db.record_run_finish(conn, run_id, {
                "stage": "enrich_linkedin",
                "considered": summary.considered,
                "enriched": summary.enriched,
                "from_cache": summary.from_cache,
                "failed": summary.failed,
                "refiltered": summary.refiltered,
            })
        conn.close()

    return summary


def _resolve_contract(employment_type: str | None) -> str | None:
    """Map LinkedIn's 'Employment type' to our contract vocabulary, or None.

    LinkedIn states a *schedule* ("Full-time", "Part-time"), not a French
    contract. "Contract"/"Temporary" do carry contract meaning; run the value
    through the same ``parse_contract_type`` the adapters use, which returns
    None for a schedule like "full time" — so we never invent a CDI the posting
    didn't state (that would flip needs_review into a pass on fabricated data).
    """
    if not employment_type:
        return None
    # Normalize "Full-time" -> "full time" for the alias table.
    key = employment_type.strip().lower().replace("-", " ")
    return normalize.parse_contract_type(key)


def _refilter_row(conn, job_id: int, prefs: dict, summary: EnrichSummary) -> None:
    """Re-judge one just-enriched row and persist the new verdict."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    verdict = apply_hard_filters(JobRecord.from_row(row), prefs)
    db.record_filter_verdict(
        conn, job_id,
        filter_status=verdict.outcome.value,
        filter_reasons_json=verdict.reasons_json(),
    )
    summary.refiltered += 1
    key = verdict.outcome.value
    summary.refilter_status_changes[key] = summary.refilter_status_changes.get(key, 0) + 1
    if verdict.outcome is Outcome.PASSED:
        logger.info("LinkedIn %s now PASSES after enrichment", row["external_id"])


# --------------------------------------------------------------------------
# Fetching + cache
# --------------------------------------------------------------------------


def _cache_path(cache_dir: Path, job_id: str) -> Path | None:
    """Return the cache file for a job if it exists, else None."""
    path = cache_dir / f"{job_id}.html"
    return path if path.exists() else None


def _get_html(
    client: httpx.Client,
    job_id: str,
    cache_dir: Path,
    *,
    dry_run: bool,
    delay: tuple[float, float],
    skip_delay_before_first: bool,
) -> tuple[str | None, bool]:
    """Return (html, was_cached) for a job, fetching only if not cached.

    A cached response is read from disk (no network, no delay). Otherwise we
    sleep a jittered delay (unless this is the first network call of the run)
    and GET the guest endpoint. Any non-200 or transport error returns
    ``(None, False)`` — fail-soft; the caller leaves the row unenriched.
    """
    cached = _cache_path(cache_dir, job_id)
    if cached is not None:
        return cached.read_text(encoding="utf-8"), True

    if not skip_delay_before_first:
        time.sleep(random.uniform(*delay))

    url = GUEST_URL.format(id=job_id)
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("fetch error for LinkedIn %s: %s", job_id, exc)
        return None, False

    if resp.status_code != 200:
        # 429 (rate limited) or anything else — fail-soft, don't cache a failure.
        logger.warning("LinkedIn %s returned %s (skipped)", job_id, resp.status_code)
        return None, False

    html = resp.text
    # Cache the raw response even in dry-run: we already paid for the fetch, and
    # caching keeps a later real run from re-hitting the endpoint.
    (cache_dir / f"{job_id}.html").write_text(html, encoding="utf-8")
    return html, False

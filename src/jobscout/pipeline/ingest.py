"""Ingestion orchestration: fetch every source, dedupe, persist idempotently.

This is the top of the pipeline and the concrete embodiment of Phase 1's
learning goal. It ties the pieces together:

    for each enabled source: adapter.fetch()  -> list[JobRecord]
    concatenate, drop intra-batch (source, external_id) duplicates
    upsert into SQLite  -> counts (new vs. already-known)
    record the run in the ledger

Two design choices worth calling out:

  * **Fail-soft per source.** One source raising (a 500 from an API, an IMAP
    hiccup) must not abort the whole run — the others still ingest. We catch
    per-source, record the failure in the summary, and carry on. This mirrors
    the brief's "fail gracefully when blocked."

  * **`--dry-run` writes nothing.** It fetches and computes *what would* be
    inserted, but never opens/writes the DB. This lets the owner see a run's
    effect before committing it, and keeps the ingest logic honest about
    where its side effects are (only `upsert_jobs`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from jobscout.adapters import france_travail, linkedin_email, wttj
from jobscout.models import JobRecord
from jobscout.pipeline import ProgressFn
from jobscout.storage import db

# The source registry: name -> zero-arg-ish fetch callable. Keeping this a
# plain dict (not a plugin system) is deliberate — three sources don't need
# machinery, and the map doubles as the list of valid --source values.
SourceFetch = Callable[..., list[JobRecord]]

SOURCES: dict[str, SourceFetch] = {
    "wttj": wttj.fetch,
    "france_travail": france_travail.fetch,
    "linkedin_email": linkedin_email.fetch,
}


@dataclass
class SourceOutcome:
    """Per-source result line for the summary."""

    fetched: int = 0
    new: int = 0
    seen_again: int = 0
    failed: bool = False
    error: str | None = None


@dataclass
class IngestSummary:
    """The whole run's result: per-source outcomes + a dry-run flag."""

    dry_run: bool
    per_source: dict[str, SourceOutcome] = field(default_factory=dict)

    @property
    def total_new(self) -> int:
        return sum(o.new for o in self.per_source.values())

    @property
    def total_fetched(self) -> int:
        return sum(o.fetched for o in self.per_source.values())


def run_ingest(
    *,
    sources: list[str] | None = None,
    limit: int = 200,
    dry_run: bool = False,
    db_path=db.DEFAULT_DB_PATH,
    on_progress: ProgressFn | None = None,
) -> IngestSummary:
    """Fetch the given sources and persist new offers idempotently.

    `sources` defaults to all registered sources. `limit` is the per-source
    hard cap (a safety rail, per the brief's "max jobs per run"). Returns an
    IngestSummary; the CLI renders it.

    ``on_progress`` (webapp runner) reports progress **per source** — the unit is
    a source, not an offer, because total-fetched isn't knowable until each fetch
    returns, whereas the source count is known upfront.
    """
    selected = sources or list(SOURCES)
    summary = IngestSummary(dry_run=dry_run)

    # Open the DB once (unless dry-run, which never touches it) and record the
    # run so the ledger captures even a run that every source fails.
    conn = None if dry_run else db.connect(db_path)
    run_id = None
    if conn is not None:
        run_id = db.record_run_start(conn, dry_run=dry_run)

    try:
        total = len(selected)
        for i, name in enumerate(selected, 1):
            summary.per_source[name] = _ingest_one(name, limit, dry_run, conn)
            if on_progress is not None:
                on_progress(i, total)
    finally:
        if conn is not None and run_id is not None:
            counts = {
                name: {"new": o.new, "seen_again": o.seen_again,
                       "fetched": o.fetched, "failed": o.failed}
                for name, o in summary.per_source.items()
            }
            db.record_run_finish(conn, run_id, counts)
            conn.close()

    return summary


def _ingest_one(
    name: str, limit: int, dry_run: bool, conn
) -> SourceOutcome:
    """Fetch + persist one source, catching failures so others proceed."""
    fetch = SOURCES.get(name)
    if fetch is None:
        return SourceOutcome(failed=True, error=f"unknown source '{name}'")

    try:
        records = fetch(limit=limit)
    except Exception as exc:  # noqa: BLE001 — fail-soft is the whole point.
        # Keep the message; the traceback is in the exception if the caller
        # wants it. One dead source must not sink the run.
        return SourceOutcome(failed=True, error=f"{type(exc).__name__}: {exc}")

    deduped = _dedupe_batch(records)

    if dry_run or conn is None:
        # Report what *would* be new without writing. We can't know for sure
        # without the DB, so dry-run reports everything fetched as the batch
        # size; "new" is left at 0 to signal "not persisted".
        return SourceOutcome(fetched=len(deduped), new=0, seen_again=0)

    result = db.upsert_jobs(conn, deduped)
    return SourceOutcome(
        fetched=len(deduped),
        new=result.inserted,
        seen_again=result.seen_again,
    )


def _dedupe_batch(records: list[JobRecord]) -> list[JobRecord]:
    """Drop duplicate (source, external_id) within a single fetch.

    Sources legitimately return the same offer twice (WTTJ's Algolia index
    doubles hits; LinkedIn digests overlap day to day). Collapsing here keeps
    the upsert's counts meaningful and avoids redundant DB round-trips. First
    occurrence wins.
    """
    seen: set[tuple[str, str]] = set()
    out: list[JobRecord] = []
    for record in records:
        key = (record.source, record.external_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out

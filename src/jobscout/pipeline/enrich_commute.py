"""Address + commute enrichment orchestration.

The deterministic stage that runs after the hard filters and before LLM scoring:
for each offer that passed (or needs review), resolve the office address, compute
the three commute strategies from the owner's home, and persist the result. It is
the counterpart to ``filter_stage.py`` and ``enrich_linkedin.py`` and follows the
same skeleton on purpose (this is a learning project — the stages rhyme):

  * **Idempotent by DB state.** "To enrich" == passed/needs_review AND
    ``enriched_at IS NULL``. A re-run only touches newly-passed offers;
    ``--re-enrich`` re-does all (use after changing commute prefs / home).
  * **Fail-soft per row.** A geocode miss, a routing failure, or a malformed
    record must not sink the batch — the row is counted as failed/approximate and
    the run continues. An offer we can't route is simply left with a NULL commute
    for the scorer to note; never a crash.
  * **Injectable clients.** The BAN geocoding client and the Google routing
    client are parameters, so every test runs fully offline (no network).

SECURITY (CLAUDE.md): the home coordinates are resolved once here and passed only
into the routing calls. They are never written to the DB, never logged, never
placed in a summary. This module logs offer titles and commute *minutes* only.
The home geocoding itself uses BAN (the same single external routing recipient
rule applies to Google, which sees home coords for routing) — see
``docs/enrichment.md`` for the documented provider consolidation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from jobscout import config
from jobscout.enrich import address as address_mod
from jobscout.enrich import geocode
from jobscout.enrich import routing
from jobscout.enrich.routing import Coordinates
from jobscout.models import JobRecord
from jobscout.storage import db

logger = logging.getLogger("jobscout.enrich_commute")


@dataclass
class EnrichCommuteSummary:
    """The run's tally, rendered by the CLI."""

    dry_run: bool = False
    re_enrich: bool = False
    considered: int = 0      # rows selected for enrichment
    enriched: int = 0        # rows we routed and wrote a commute for
    remote_skipped: int = 0  # fully-remote → commute 0, no routing
    approximate: int = 0     # of enriched, those on an approximate/low address
    failed: int = 0          # unresolved address or routing failure (fail-soft)


def run_enrich_commute(
    *,
    prefs_path=config.DEFAULT_PREFERENCES_PATH,
    env_path=config.DEFAULT_ENV_PATH,
    db_path=db.DEFAULT_DB_PATH,
    limit: int | None = None,
    re_enrich: bool = False,
    dry_run: bool = False,
    _geo_client: httpx.Client | None = None,
    _routing_client: httpx.Client | None = None,
) -> EnrichCommuteSummary:
    """Enrich passed/needs_review offers with address + commute; persist.

    Resolves the home origin once, then per offer: resolve address → (remote?
    commute 0 : route the three strategies) → write. ``dry_run`` computes and
    reports but writes nothing. Returns an ``EnrichCommuteSummary`` the CLI
    renders. Opens the DB even in dry-run (the offers live there).
    """
    prefs = config.load_preferences(prefs_path)
    env = config.load_env(env_path)
    commute_prefs = config.commute_prefs(prefs)
    summary = EnrichCommuteSummary(dry_run=dry_run, re_enrich=re_enrich)

    conn = db.connect(db_path)
    run_id = None if dry_run else db.record_run_start(conn, dry_run=dry_run)
    owns_geo = _geo_client is None
    owns_routing = _routing_client is None
    geo_client = _geo_client or httpx.Client(timeout=15.0)
    routing_client = _routing_client or httpx.Client(timeout=20.0)

    try:
        # Resolve home + key ONCE. A missing key/home fails the whole run loudly
        # (nothing routable), which is better than silently marking all failed.
        api_key = config.google_routes_key(env)
        home = _resolve_home(prefs, geo_client)

        rows = db.select_jobs_to_enrich(conn, limit=limit, re_enrich=re_enrich)
        summary.considered = len(rows)

        for row in rows:
            _enrich_row(
                conn, row, home=home, api_key=api_key, prefs=commute_prefs,
                geo_client=geo_client, routing_client=routing_client,
                dry_run=dry_run, summary=summary,
            )
        if not dry_run:
            conn.commit()
    finally:
        if owns_geo:
            geo_client.close()
        if owns_routing:
            routing_client.close()
        if run_id is not None:
            db.record_run_finish(conn, run_id, {
                "stage": "enrich_commute",
                "considered": summary.considered,
                "enriched": summary.enriched,
                "remote_skipped": summary.remote_skipped,
                "approximate": summary.approximate,
                "failed": summary.failed,
            })
        conn.close()

    return summary


def _resolve_home(prefs: dict, geo_client: httpx.Client) -> Coordinates:
    """Resolve the home origin to coordinates once (coords win over address).

    Raises a clear error if home can't be resolved — the run has no origin, so
    every route would fail; better to stop loudly than mark the whole batch
    failed. The resolved coordinates never leave this process except inside a
    routing request body.
    """
    home = config.home_location(prefs)
    if home.has_coords:
        return Coordinates(lat=home.lat, lon=home.lon)
    if home.address:
        hit = geocode.geocode(home.address, client=geo_client)
        if hit is not None:
            return Coordinates(lat=hit.lat, lon=hit.lon)
    raise RuntimeError(
        "Home location could not be resolved. Set home.lat/home.lon or a "
        "geocodable home.address in preferences.yaml."
    )


def _enrich_row(
    conn, row, *, home, api_key, prefs, geo_client, routing_client, dry_run, summary,
) -> None:
    """Resolve + route one row, fail-soft. Updates ``summary`` and persists."""
    try:
        record = JobRecord.from_row(row)
        resolved = address_mod.resolve_address(record, client=geo_client)

        # Fully-remote → commute 0, no routing.
        if resolved.is_remote:
            summary.remote_skipped += 1
            summary.enriched += 1
            _log_row(row, "remote", 0.0)
            if not dry_run:
                db.record_enrichment(
                    conn, row["id"], address=None, lat=None, lon=None,
                    address_source="remote", address_confidence=None,
                    commute_minutes=0.0, commute_mode="remote",
                    commute_strategies_json=None,
                )
            return

        if not resolved.is_resolved:
            summary.failed += 1
            _log_row(row, "unresolved", None)
            return  # leave the row unenriched — a later run can retry.

        plan = routing.plan_commute(
            home, Coordinates(lat=resolved.lat, lon=resolved.lon),
            api_key=api_key, prefs=prefs, client=routing_client,
        )
        if plan.best_minutes is None:
            summary.failed += 1
            _log_row(row, resolved.source, None)
            return

        summary.enriched += 1
        if resolved.confidence == "low" or resolved.source == "approximate":
            summary.approximate += 1
        _log_row(row, resolved.source, plan.best_minutes)

        if not dry_run:
            db.record_enrichment(
                conn, row["id"],
                address=resolved.address, lat=resolved.lat, lon=resolved.lon,
                address_source=resolved.source,
                address_confidence=resolved.confidence,
                commute_minutes=plan.best_minutes, commute_mode=plan.best_mode,
                commute_strategies_json=json.dumps(plan.as_json_dict(), ensure_ascii=False),
            )
    except Exception as exc:  # noqa: BLE001 — per-row fail-soft is the point.
        summary.failed += 1
        logger.warning(
            "enrich error [%s] %r: %s", row["source"], row["title"], type(exc).__name__
        )


def _log_row(row, source: str, minutes: float | None) -> None:
    """One transparent line per offer: source + commute minutes (never coords)."""
    mins = "?" if minutes is None else f"{minutes:.0f}min"
    logger.info(
        "[%s] %r @ %r — address=%s commute=%s",
        row["source"], row["title"], row["company"], source, mins,
    )

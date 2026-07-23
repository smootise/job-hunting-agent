"""Dashboard aggregates: counts for the webapp's overview, plus the run ledger.

The dashboard answers two questions the ``jobs``/``runs`` tables already hold
the data for: "where do offers sit in the pipeline?" and "when did we last run
anything?". Neither has a helper in ``db.py`` — every ``select_*`` there returns
full rows for a stage to process, not counts for a human to read. So this is a
small read-only aggregation layer.

**Predicate parity with the worklists is the whole point.** Each "pending"
count below mirrors a ``db.select_jobs_to_*`` predicate exactly, because the
dashboard is telling the owner what the next CLI run *would* pick up. We
deliberately duplicate the ``WHERE`` clause here (as ``COUNT``) rather than
calling the ``select_*`` functions and taking ``len()`` — those do ``SELECT *``
and would haul every column just to count. The duplication is guarded by a test
(``tests/test_stats.py``) that asserts each count equals ``len(select_jobs_to_*)``
on the same DB, so the two can't silently drift.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# The pipeline worklist predicates, mirrored from db.py's select_jobs_to_* so
# the dashboard's "pending" numbers match what a CLI run would process. Keep
# each string in sync with its named counterpart; the parity test enforces it.
_ELIGIBLE = "filter_status IN ('passed', 'needs_review')"  # scorer judges both tails
_PENDING = {
    # select_jobs_to_enrich: eligible AND not yet commute-enriched.
    "to_enrich": f"{_ELIGIBLE} AND enriched_at IS NULL",
    # select_jobs_to_score: eligible AND not yet scored (independent of enrich).
    "to_score": f"{_ELIGIBLE} AND scored_at IS NULL",
    # select_jobs_to_research_company: eligible AND not yet researched.
    "to_research_company": f"{_ELIGIBLE} AND company_researched_at IS NULL",
    # select_jobs_to_research_address: eligible AND not already agent-placed.
    # (SQL only narrows to candidates; resolve_address is the true authority — so
    # this count is an upper bound on what the agent might look at, same as the
    # worklist SQL, not the final "needs an agent" number.)
    "to_research_address": f"{_ELIGIBLE} AND (address_source IS NULL OR address_source != 'agent')",
}


@dataclass(frozen=True)
class DashboardStats:
    """The agent-pipeline section's numbers: the funnel plus per-stage backlog."""

    total: int
    by_filter_status: dict[str, int]  # keys: passed/needs_review/rejected/unfiltered
    scored: int
    to_filter: int
    to_enrich: int
    to_score: int
    to_research_company: int
    to_research_address: int


@dataclass(frozen=True)
class LastRun:
    """One row of the ``runs`` ledger, with ``source_counts_json`` parsed.

    ``stage`` is the pipeline stage name for non-ingest runs (filter/score/…),
    read from the ``"stage"`` key every non-ingest stage stamps into
    ``source_counts_json``; it is None for an ingest run (which instead stores
    per-source ``{new, seen_again, …}`` counts under ``source_counts``).
    """

    started_at: str | None
    finished_at: str | None
    stage: str | None
    source_counts: dict[str, Any]
    dry_run: bool


@dataclass(frozen=True)
class ReviewStats:
    """The 'My review' dashboard section: where the owner is in their own process.

    ``unreviewed`` is offers with no ``offer_review`` row yet — the triage backlog.
    """

    to_review: int
    applied: int
    not_interested: int
    unreviewed: int


def _count(conn: sqlite3.Connection, where: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM jobs"
    if where:
        sql += " WHERE " + where
    return int(conn.execute(sql).fetchone()[0])


def dashboard_stats(conn: sqlite3.Connection) -> DashboardStats:
    """Compute every dashboard number in a handful of COUNT queries."""
    by_status: dict[str, int] = {
        "passed": 0,
        "needs_review": 0,
        "rejected": 0,
        "unfiltered": 0,
    }
    for row in conn.execute(
        "SELECT filter_status, COUNT(*) AS n FROM jobs GROUP BY filter_status"
    ):
        key = row["filter_status"] or "unfiltered"
        by_status[key] = by_status.get(key, 0) + int(row["n"])

    return DashboardStats(
        total=_count(conn),
        by_filter_status=by_status,
        scored=_count(conn, "score_status = 'scored'"),
        # to_filter mirrors select_unfiltered_jobs (filter_status IS NULL).
        to_filter=_count(conn, "filter_status IS NULL"),
        to_enrich=_count(conn, _PENDING["to_enrich"]),
        to_score=_count(conn, _PENDING["to_score"]),
        to_research_company=_count(conn, _PENDING["to_research_company"]),
        to_research_address=_count(conn, _PENDING["to_research_address"]),
    )


def review_stats(conn: sqlite3.Connection) -> ReviewStats:
    """Count offers per the owner's disposition, plus the unreviewed backlog.

    Mirrors ``dashboard_stats``'s ``by_filter_status`` GROUP BY loop. ``_count``
    isn't reused — it's hardcoded ``FROM jobs`` and these count ``offer_review``.
    Unknown/legacy disposition values are ignored (defensive), matching how the
    filter loop buckets only known keys.
    """
    counts = {"to_review": 0, "applied": 0, "not_interested": 0}
    for row in conn.execute(
        "SELECT disposition, COUNT(*) AS n FROM offer_review GROUP BY disposition"
    ):
        key = row["disposition"]
        if key in counts:
            counts[key] = int(row["n"])
    unreviewed = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id NOT IN (SELECT job_id FROM offer_review)"
        ).fetchone()[0]
    )
    return ReviewStats(**counts, unreviewed=unreviewed)


def _row_to_last_run(row: sqlite3.Row) -> LastRun:
    counts: dict[str, Any] = {}
    if row["source_counts_json"]:
        try:
            counts = json.loads(row["source_counts_json"])
        except (ValueError, TypeError):
            counts = {}
    stage = counts.pop("stage", None) if isinstance(counts, dict) else None
    return LastRun(
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        stage=stage,
        source_counts=counts if isinstance(counts, dict) else {},
        dry_run=bool(row["dry_run"]),
    )


def last_run(conn: sqlite3.Connection) -> LastRun | None:
    """The most recently started run, or None if the ledger is empty."""
    row = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return _row_to_last_run(row) if row is not None else None


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[LastRun]:
    """The last ``limit`` runs, newest first — for a small ledger table."""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_last_run(r) for r in rows]

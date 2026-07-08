"""Hard-filter orchestration: read stored offers, judge them, persist verdicts.

The deterministic counterpart to Phase 1's ``ingest``. Where ingest turns
external sources into stored ``JobRecord``s, this stage reads those records back
out and stamps each with a hard-filter verdict (passed / needs_review /
rejected). It is the cheap, transparent gate that runs *before* the expensive
enrichment + LLM scoring stages, so those only ever see offers worth the cost.

Two design choices mirror ``ingest.py`` deliberately (this is a learning
project — the stages should rhyme):

  * **Idempotent by DB state.** "To be filtered" == ``filter_status IS NULL``.
    A re-run judges only newly ingested offers; ``--refilter`` re-judges every
    offer (use after editing preferences.yaml). Same "novelty lives in the DB"
    lesson as ingest.

  * **Fail-soft per row.** A single malformed record must not sink the batch:
    we catch per row and record it as ``needs_review`` with an error reason,
    the per-record analog of ingest's per-source fail-soft.

One asymmetry worth calling out vs. ingest: this stage's ``--dry-run`` still
*opens and reads* the DB (it has to, to see the offers) — it just writes
nothing. Ingest's dry-run skips the DB entirely because its inputs are the
external sources, not the DB.

Every rejection is logged (one line, naming the offer and the firing filters)
because a silent drop of a good offer is the worst outcome in this project.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jobscout import config
from jobscout.models import JobRecord
from jobscout.pipeline.filters import (
    FilterReason,
    FilterVerdict,
    Outcome,
    apply_hard_filters,
)
from jobscout.storage import db

logger = logging.getLogger("jobscout.filter")


@dataclass
class FilterSummary:
    """The run's tally, rendered by the CLI."""

    dry_run: bool
    refilter: bool = False
    passed: int = 0
    needs_review: int = 0
    rejected: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.needs_review + self.rejected

    def _bump(self, outcome: Outcome) -> None:
        if outcome is Outcome.PASSED:
            self.passed += 1
        elif outcome is Outcome.NEEDS_REVIEW:
            self.needs_review += 1
        else:
            self.rejected += 1


def run_filter(
    *,
    prefs_path=config.DEFAULT_PREFERENCES_PATH,
    db_path=db.DEFAULT_DB_PATH,
    limit: int | None = None,
    refilter: bool = False,
    dry_run: bool = False,
) -> FilterSummary:
    """Apply the hard filters to stored offers and persist the verdicts.

    Reads offers awaiting judgement (or all, when ``refilter``), runs
    ``apply_hard_filters`` on each, and — unless ``dry_run`` — writes the
    verdict back. Returns a ``FilterSummary``; the CLI renders it. Opens the DB
    even in dry-run because the offers to judge live there.
    """
    prefs = config.load_preferences(prefs_path)
    summary = FilterSummary(dry_run=dry_run, refilter=refilter)

    conn = db.connect(db_path)
    run_id = None if dry_run else db.record_run_start(conn, dry_run=dry_run)
    try:
        rows = db.select_unfiltered_jobs(conn, limit=limit, refilter=refilter)
        for row in rows:
            verdict = _judge_row(row, prefs)
            summary._bump(verdict.outcome)
            if verdict.outcome is not Outcome.PASSED:
                _log_verdict(row, verdict)
            if not dry_run:
                db.record_filter_verdict(
                    conn,
                    row["id"],
                    filter_status=verdict.outcome.value,
                    filter_reasons_json=verdict.reasons_json(),
                )
        if not dry_run:
            conn.commit()
    finally:
        if run_id is not None:
            db.record_run_finish(
                conn,
                run_id,
                {
                    "stage": "filter",
                    "passed": summary.passed,
                    "needs_review": summary.needs_review,
                    "rejected": summary.rejected,
                    "refilter": refilter,
                },
            )
        conn.close()

    return summary


def _judge_row(row, prefs: dict) -> FilterVerdict:
    """Judge one DB row, fail-soft: a malformed record → needs_review, not a
    crash that aborts the batch."""
    try:
        record = JobRecord.from_row(row)
        return apply_hard_filters(record, prefs)
    except Exception as exc:  # noqa: BLE001 — per-row fail-soft is the point.
        return FilterVerdict(
            Outcome.NEEDS_REVIEW,
            (
                FilterReason(
                    "error",
                    Outcome.NEEDS_REVIEW,
                    f"filter error: {type(exc).__name__}: {exc}",
                ),
            ),
        )


def _log_verdict(row, verdict: FilterVerdict) -> None:
    """One transparent log line per non-passing offer.

    Names the offer and every firing filter so a human can audit why an offer
    was dropped or flagged — never a silent drop."""
    fired = "; ".join(f"{r.filter}={r.outcome.value}: {r.reason}" for r in verdict.reasons)
    logger.info(
        "%s [%s] %r @ %r — %s",
        verdict.outcome.value.upper(),
        row["source"],
        row["title"],
        row["company"],
        fired,
    )

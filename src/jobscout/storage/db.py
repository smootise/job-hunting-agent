"""SQLite storage: the single source of truth, and the home of idempotency.

This module is where Phase 1's core lesson — *agent state and idempotency* —
actually lives. Two ideas do all the work:

1. **"New since last run" == "not already in the DB."** Every source is
   re-fetched in full on every run; sources have no notion of "give me only
   what changed." So novelty is decided *here*, by whether a
   `(source, external_id)` row already exists. A `UNIQUE(source, external_id)`
   constraint makes that a database-enforced fact, not application hope.

2. **Re-running must be safe.** Run the pipeline twice in a row and the second
   run must insert zero rows and merely bump `last_seen_at` on the ones it saw
   again. That is what "idempotent" means for this project, and `upsert_jobs`
   is built to guarantee it.

Cross-source dedupe (the same job on WTTJ *and* LinkedIn) is a softer, second
layer: we keep both physical rows but stamp them with a shared `dup_group`
so a later stage can collapse them for display. We deliberately do NOT merge
them into one row — that would lose each source's distinct URL/description,
and embedding-based similarity is explicitly a v2 concern (CLAUDE.md backlog).
The v1 matcher is a plain equality check on normalized (company, title).

Everything here is ordinary parameterized SQL — no ORM. The schema doubles as
documentation of what the pipeline knows about a job.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jobscout import normalize
from jobscout.models import JobRecord

DEFAULT_DB_PATH = Path("data/jobs.db")


def _utcnow() -> str:
    """Timezone-aware UTC ISO-8601 timestamp — the one time format we store."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

# Columns fall into two groups:
#   - the 11 source fields from JobRecord (the adapter contract), and
#   - bookkeeping the pipeline owns: surrogate id, first/last seen timestamps,
#     the normalized dedupe keys, dup_group, status, and the Phase 2
#     hard-filter verdict (filter_status/filter_reasons/filtered_at).
# Later phases add enrichment/scoring columns the same way. Additions are
# purely additive; `_migrate_add_columns` backfills them on an existing DB.
#
# Note the deliberate separation of `status` (offer lifecycle: 'new', later
# 'scored'/'drafted') from `filter_status` ('passed'|'needs_review'|'rejected',
# NULL until filtered). They are orthogonal concerns — overloading one column
# would entangle the filter stage with everything downstream.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT    NOT NULL,
    external_id        TEXT    NOT NULL,
    url                TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    company            TEXT    NOT NULL,
    location           TEXT,
    contract_type      TEXT,
    salary_text        TEXT,
    description        TEXT,
    posted_at          TEXT,
    lang               TEXT,
    normalized_company TEXT    NOT NULL,
    normalized_title   TEXT    NOT NULL,
    dup_group          INTEGER,
    status             TEXT    NOT NULL DEFAULT 'new',
    filter_status      TEXT,
    filter_reasons     TEXT,
    filtered_at        TEXT,
    first_seen_at      TEXT    NOT NULL,
    last_seen_at       TEXT    NOT NULL,
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_fuzzy
    ON jobs (normalized_company, normalized_title);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    source_counts_json TEXT,
    dry_run            INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the jobs DB and ensure the schema exists.

    Idempotent: safe to call every run. Uses `CREATE TABLE IF NOT EXISTS`, so
    an existing DB is left as-is. Enables WAL (better concurrent-read
    behavior) and a Row factory so callers get dict-like rows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


# Columns added after the original Phase 1 schema shipped. `CREATE TABLE IF NOT
# EXISTS` won't add columns to a table that already exists, so an existing
# `data/jobs.db` from Phase 1 needs these backfilled with ALTER TABLE. Keyed by
# column name → its type/definition. Adding a Phase 3 column is a one-line edit
# here.
_ADDED_COLUMNS: dict[str, str] = {
    "filter_status": "TEXT",
    "filter_reasons": "TEXT",
    "filtered_at": "TEXT",
}


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if absent, then backfill any newer columns.

    Separated so tests can call it on an in-memory connection without touching
    the filesystem. Idempotent: a fresh DB gets the columns from `_SCHEMA`; an
    existing Phase 1 DB gets them from `_migrate_add_columns`; a current DB is
    left untouched.
    """
    conn.executescript(_SCHEMA)
    _migrate_add_columns(conn)
    conn.commit()


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN for any `_ADDED_COLUMNS` the `jobs` table lacks.

    Reads the live column set via PRAGMA and adds only what's missing, so this
    is safe to run on every connect regardless of how old the DB is. New columns
    are nullable with no default, so the ALTER is instant and existing rows read
    NULL (== 'not yet filtered'), which is exactly the intended initial state.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, definition in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")


# --------------------------------------------------------------------------
# Upsert (the idempotency guarantee) + fuzzy dup grouping
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UpsertResult:
    """Outcome of persisting a batch. `inserted` == the run's genuinely new
    offers; `seen_again` were already known and only had last_seen_at bumped."""

    inserted: int
    seen_again: int


def find_fuzzy_duplicate(
    conn: sqlite3.Connection, record: JobRecord
) -> int | None:
    """Return an existing `dup_group` for the same logical job, or None.

    "Same logical job" = identical normalized (company, title) already in the
    DB. We reuse that row's dup_group so cross-source copies share a group id.
    This is intentionally a strict equality match on normalized strings: it
    won't merge reworded titles (accepted v1 limitation), but — crucially — it
    also won't *falsely* merge unrelated jobs, which is the worse error here.
    """
    norm_company = normalize.normalize_company(record.company)
    norm_title = normalize.normalize_title(record.title)
    if not norm_company or not norm_title:
        return None
    row = conn.execute(
        """
        SELECT dup_group, id FROM jobs
        WHERE normalized_company = ? AND normalized_title = ?
        ORDER BY dup_group IS NULL, id
        LIMIT 1
        """,
        (norm_company, norm_title),
    ).fetchone()
    if row is None:
        return None
    # If a matching row exists but has no group yet, seed one from its own id
    # (stable + unique) and backfill it so both rows end up grouped.
    if row["dup_group"] is None:
        group = row["id"]
        conn.execute(
            "UPDATE jobs SET dup_group = ? WHERE id = ?", (group, row["id"])
        )
        return group
    return row["dup_group"]


def upsert_jobs(
    conn: sqlite3.Connection, records: list[JobRecord]
) -> UpsertResult:
    """Insert new offers, refresh last_seen_at on ones already known.

    The idempotency contract: calling this with the same batch twice inserts
    0 the second time. It works row-by-row so we can (a) count inserts vs.
    updates precisely and (b) compute the normalized dedupe keys and dup_group
    per record. All in one transaction — a crash mid-batch rolls back cleanly.
    """
    inserted = 0
    seen_again = 0
    now = _utcnow()

    for record in records:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
            (record.source, record.external_id),
        ).fetchone()

        if existing is not None:
            # Known offer: only touch last_seen_at. Never overwrite the stored
            # content — the first capture is the record of when/what we saw.
            conn.execute(
                "UPDATE jobs SET last_seen_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
            seen_again += 1
            continue

        # New offer. Compute dedupe keys and look for a cross-source sibling
        # to share a dup_group with.
        norm_company = normalize.normalize_company(record.company)
        norm_title = normalize.normalize_title(record.title)
        dup_group = find_fuzzy_duplicate(conn, record)

        row = record.to_row()
        conn.execute(
            """
            INSERT INTO jobs (
                source, external_id, url, title, company, location,
                contract_type, salary_text, description, posted_at, lang,
                normalized_company, normalized_title, dup_group, status,
                first_seen_at, last_seen_at
            ) VALUES (
                :source, :external_id, :url, :title, :company, :location,
                :contract_type, :salary_text, :description, :posted_at, :lang,
                :normalized_company, :normalized_title, :dup_group, 'new',
                :first_seen_at, :last_seen_at
            )
            """,
            {
                **row,
                "normalized_company": norm_company,
                "normalized_title": norm_title,
                "dup_group": dup_group,
                "first_seen_at": now,
                "last_seen_at": now,
            },
        )
        inserted += 1

    conn.commit()
    return UpsertResult(inserted=inserted, seen_again=seen_again)


# --------------------------------------------------------------------------
# Run ledger (auditability + reconstructing "new since last run")
# --------------------------------------------------------------------------


def record_run_start(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """Open a run row and return its id. Called once at the top of a run."""
    cur = conn.execute(
        "INSERT INTO runs (started_at, dry_run) VALUES (?, ?)",
        (_utcnow(), 1 if dry_run else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_run_finish(
    conn: sqlite3.Connection, run_id: int, source_counts: dict[str, object]
) -> None:
    """Close a run row with its finish time and per-source counts (as JSON)."""
    conn.execute(
        "UPDATE runs SET finished_at = ?, source_counts_json = ? WHERE id = ?",
        (_utcnow(), json.dumps(source_counts, ensure_ascii=False), run_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Phase 2 hard-filter verdicts
# --------------------------------------------------------------------------


def select_unfiltered_jobs(
    conn: sqlite3.Connection, *, limit: int | None = None, refilter: bool = False
) -> list[sqlite3.Row]:
    """Return jobs awaiting a filter verdict (or all jobs when refiltering).

    Default: rows with ``filter_status IS NULL`` — the ones not yet judged. This
    is what makes the filter stage idempotent: a re-run picks up only newly
    ingested offers, exactly like ingest's "new since last run" is decided by
    the DB. ``refilter=True`` selects every job so a preferences.yaml change can
    be re-applied to the whole table. Rows are dict-like ``sqlite3.Row`` and
    carry every column, so ``JobRecord.from_row`` can consume them directly.
    """
    sql = "SELECT * FROM jobs"
    if not refilter:
        sql += " WHERE filter_status IS NULL"
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def record_filter_verdict(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    filter_status: str,
    filter_reasons_json: str,
) -> None:
    """Persist one offer's hard-filter verdict.

    Writes the aggregate status, the reasons JSON (every firing filter, so
    nothing is dropped unexplained — the transparency invariant), and a
    timestamp. A plain UPDATE, so re-running ``--refilter`` overwrites cleanly
    and idempotently. The caller commits (one commit per batch)."""
    conn.execute(
        "UPDATE jobs SET filter_status = ?, filter_reasons = ?, filtered_at = ? "
        "WHERE id = ?",
        (filter_status, filter_reasons_json, _utcnow(), job_id),
    )

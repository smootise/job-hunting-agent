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
    address            TEXT,
    lat                REAL,
    lon                REAL,
    address_source     TEXT,
    address_confidence TEXT,
    commute_minutes    REAL,
    commute_mode       TEXT,
    commute_strategies TEXT,
    enriched_at        TEXT,
    score_total        REAL,
    score_status       TEXT,
    score_json         TEXT,
    scored_at          TEXT,
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

-- The owner's OWN application-tracking state, one row per offer they've acted on
-- (webapp V2). Deliberately a SEPARATE table, not columns on `jobs`: `jobs` is
-- pipeline-owned (ingest/filter/score write it), whereas this is human-owned and
-- must never entangle with the automated stages. `disposition` is one of
-- 'to_review' | 'applied' | 'not_interested' — the domain is enforced in Python
-- (`upsert_review`), consistent with how `filter_status`/`score_status` are
-- validated by the pipeline, not by a SQL CHECK. `applied_at` is stamped once,
-- when an offer is first marked 'applied', and preserved across later edits.
CREATE TABLE IF NOT EXISTS offer_review (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs(id),
    disposition TEXT,
    applied_at  TEXT,
    notes       TEXT,
    updated_at  TEXT
);
"""


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the jobs DB and ensure the schema exists.

    Idempotent: safe to call every run. Uses `CREATE TABLE IF NOT EXISTS`, so
    an existing DB is left as-is. Enables WAL (better concurrent-read
    behavior) and a Row factory so callers get dict-like rows.

    ``busy_timeout`` is set so a connection waits (up to 5s) for a lock rather
    than failing immediately with ``SQLITE_BUSY``. WAL already lets readers and
    one writer coexist; this covers the brief window where two *writers* overlap
    — e.g. the webapp's background pipeline runner committing a stage while a
    request handler writes a review row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
    # Phase 2 address + commute enrichment.
    "address": "TEXT",
    "lat": "REAL",
    "lon": "REAL",
    "address_source": "TEXT",
    "address_confidence": "TEXT",
    "commute_minutes": "REAL",
    "commute_mode": "TEXT",
    "commute_strategies": "TEXT",  # JSON: all three strategies + per-leg detail.
    "enriched_at": "TEXT",
    # Phase 2 LLM scoring.
    "score_total": "REAL",         # headline weighted 0-100 (for sorting the digest).
    "score_status": "TEXT",        # 'scored' | 'needs_review' (invalid JSON twice).
    "score_json": "TEXT",          # full per-criterion breakdown + reasoning + red_flags.
    "scored_at": "TEXT",           # idempotency stamp; NULL == not yet scored.
    # Phase 3 company research (advisory context for the scorer + letter agent).
    "company_brief": "TEXT",       # JSON: {summary, product, culture, size_signal, ai_usage, sources[], confidence}.
    "company_researched_at": "TEXT",  # idempotency stamp; NULL == not yet researched.
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


# --------------------------------------------------------------------------
# LinkedIn description enrichment (guest-endpoint backfill)
# --------------------------------------------------------------------------


def select_jobs_missing_description(
    conn: sqlite3.Connection,
    *,
    source: str = "linkedin_email",
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Return rows for one source that have no stored description yet.

    The enrichment step's work list. Selecting on ``description IS NULL`` is
    what makes the fetch cheap and idempotent: an offer we already backfilled is
    skipped, so a re-run only touches jobs still missing prose. This is the same
    "novelty lives in the DB" idempotency the ingest and filter stages use, and
    it doubles as the fetch cache — we never re-hit a job we've enriched. Rows
    carry every column, so ``JobRecord.from_row`` consumes them directly.
    """
    sql = "SELECT * FROM jobs WHERE source = ? AND (description IS NULL OR description = '')"
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (source, limit)).fetchall()
    return conn.execute(sql, (source,)).fetchall()


def backfill_description(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    description: str,
    contract_type: str | None = None,
) -> None:
    """Fill in a fetched ``description`` (and optionally ``contract_type``).

    Only ever *adds* information: writes the description, and updates
    ``contract_type`` only when a real one was resolved (``None`` leaves the
    existing value untouched — we never overwrite a stated contract with
    nothing, nor invent one). Never touches the original source fields beyond
    these. The caller commits.
    """
    if contract_type is not None:
        conn.execute(
            "UPDATE jobs SET description = ?, contract_type = ? WHERE id = ?",
            (description, contract_type, job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET description = ? WHERE id = ?",
            (description, job_id),
        )


# --------------------------------------------------------------------------
# Phase 2 address + commute enrichment
# --------------------------------------------------------------------------


def select_jobs_to_enrich(
    conn: sqlite3.Connection, *, limit: int | None = None, re_enrich: bool = False
) -> list[sqlite3.Row]:
    """Return offers awaiting commute enrichment (or all enrichable, re-enrich).

    Default: rows that passed the hard filters (``filter_status`` in
    ``passed``/``needs_review`` — the scorer judges the needs_review tail too, so
    both deserve a commute) and haven't been enriched yet (``enriched_at IS
    NULL``). This is the stage's idempotency, same shape as ``select_unfiltered_
    jobs``: a re-run only picks up newly-passed offers. ``re_enrich=True`` selects
    every enrichable row so a preferences.yaml commute change can be re-applied.
    Rejected offers are never enriched (we don't spend API calls on them). Rows
    carry every column, so ``JobRecord.from_row`` consumes them directly.
    """
    sql = "SELECT * FROM jobs WHERE filter_status IN ('passed', 'needs_review')"
    if not re_enrich:
        sql += " AND enriched_at IS NULL"
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def record_enrichment(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    address: str | None,
    lat: float | None,
    lon: float | None,
    address_source: str | None,
    address_confidence: str | None,
    commute_minutes: float | None,
    commute_mode: str | None,
    commute_strategies_json: str | None,
) -> None:
    """Persist one offer's resolved address + commute result.

    A plain UPDATE stamping ``enriched_at`` so the row drops out of
    ``select_jobs_to_enrich`` on the next run (idempotency) and ``--re-enrich``
    overwrites cleanly. Stores the headline ``commute_minutes``/``commute_mode``
    for sorting/scoring plus the full ``commute_strategies`` JSON for review. The
    caller commits (one commit per batch). NOTE: only commute *minutes* and the
    (public) office address are stored here — never the home coordinates.
    """
    conn.execute(
        "UPDATE jobs SET address = ?, lat = ?, lon = ?, address_source = ?, "
        "address_confidence = ?, commute_minutes = ?, commute_mode = ?, "
        "commute_strategies = ?, enriched_at = ? WHERE id = ?",
        (
            address, lat, lon, address_source, address_confidence,
            commute_minutes, commute_mode, commute_strategies_json,
            _utcnow(), job_id,
        ),
    )


# --------------------------------------------------------------------------
# Phase 2 LLM scoring
# --------------------------------------------------------------------------


def select_jobs_to_score(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    rescore: bool = False,
    ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    """Return offers awaiting an LLM score (or all scoreable, when rescoring).

    Default: rows that passed the hard filters (``filter_status`` in
    ``passed``/``needs_review`` — the scorer judges the needs_review tail too)
    and haven't been scored yet (``scored_at IS NULL``). Same idempotency shape
    as ``select_jobs_to_enrich``: a re-run only picks up newly-passed offers.
    Crucially this does NOT require ``enriched_at`` — scoring is independent of
    enrichment; a row with a NULL commute is scored with the commute criterion
    flagged unknown, and a later ``--rescore`` folds the commute in once an
    address resolves. ``rescore=True`` selects every scoreable row so a
    preferences.yaml rubric change (or a fresh commute) can be re-applied.

    ``ids`` restricts to a specific set of job ids (still gated on the eligibility
    predicate — a rejected/unfiltered id is silently excluded, never scored). It
    is the targeted-run primitive the webapp will drive; when given, the
    ``scored_at IS NULL`` gate is dropped (an explicit id list is an explicit
    "score these", like ``rescore`` but scoped). Rows carry every column, so
    ``JobRecord.from_row`` consumes them directly.
    """
    sql = "SELECT * FROM jobs WHERE filter_status IN ('passed', 'needs_review')"
    params: list[object] = []
    if ids is not None:
        if not ids:
            return []  # explicit empty selection → nothing, not "everything".
        placeholders = ", ".join("?" for _ in ids)
        sql += f" AND id IN ({placeholders})"
        params.extend(ids)
    elif not rescore:
        # An explicit id list already means "score these"; the unscored gate
        # only applies to the untargeted default run.
        sql += " AND scored_at IS NULL"
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def record_score(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    score_total: float | None,
    score_status: str,
    score_json: str | None,
) -> None:
    """Persist one offer's LLM score.

    A plain UPDATE stamping ``scored_at`` so the row drops out of
    ``select_jobs_to_score`` next run (idempotency) and ``--rescore`` overwrites
    cleanly. ``score_total`` is the headline weighted 0-100 (NULL when the row
    is ``needs_review`` because the model never produced valid JSON);
    ``score_json`` is the full breakdown for the digest/review. The caller
    commits (one commit per batch). No home/commute origin is ever stored here —
    ``score_json`` carries commute *minutes*-derived sub-scores only, never a
    location."""
    conn.execute(
        "UPDATE jobs SET score_total = ?, score_status = ?, score_json = ?, "
        "scored_at = ? WHERE id = ?",
        (score_total, score_status, score_json, _utcnow(), job_id),
    )


# --------------------------------------------------------------------------
# Webapp V2 — the owner's own application-tracking state (offer_review)
# --------------------------------------------------------------------------

# The valid dispositions. Enforced here (Python) rather than a SQL CHECK, so the
# domain lives next to the writer — same choice as filter_status/score_status.
REVIEW_DISPOSITIONS = ("to_review", "applied", "not_interested")


def upsert_review(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    disposition: str,
    notes: str | None = None,
) -> None:
    """Insert or update one offer's human application-tracking row.

    Unlike the ``record_*`` helpers (which UPDATE a column on an existing ``jobs``
    row), this targets the fresh ``offer_review`` table, so it's an upsert:
    ``INSERT ... ON CONFLICT(job_id) DO UPDATE``. The caller commits (same
    contract as ``record_score`` — no internal commit).

    ``applied_at`` is a *record* of when the offer was first marked applied, so it
    is **preserved** across later edits: we read any existing row first and keep
    its ``applied_at`` when the offer is (still) applied. It's stamped fresh only
    on the transition *into* applied from no prior stamp, and cleared when the
    disposition is not applied. This means editing the notes of an already-applied
    offer never moves its applied date.

    Raises ``ValueError`` for an unknown ``disposition`` (guards the caller/route).
    No home/location data is involved — this table holds only the owner's own
    review state.
    """
    if disposition not in REVIEW_DISPOSITIONS:
        raise ValueError(
            f"unknown disposition {disposition!r}; expected one of {REVIEW_DISPOSITIONS}"
        )

    now = _utcnow()
    if disposition == "applied":
        existing = conn.execute(
            "SELECT applied_at FROM offer_review WHERE job_id = ?", (job_id,)
        ).fetchone()
        prior = existing["applied_at"] if existing is not None else None
        applied_at = prior or now  # keep the original stamp; set one if absent
    else:
        applied_at = None  # not applied → no applied date

    conn.execute(
        "INSERT INTO offer_review (job_id, disposition, applied_at, notes, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "disposition = excluded.disposition, "
        "applied_at = excluded.applied_at, "
        "notes = excluded.notes, "
        "updated_at = excluded.updated_at",
        (job_id, disposition, applied_at, notes, now),
    )


# --------------------------------------------------------------------------
# Phase 3 agents — company research + agent-resolved address
# --------------------------------------------------------------------------


def select_jobs_to_research_company(
    conn: sqlite3.Connection, *, limit: int | None = None, redo: bool = False
) -> list[sqlite3.Row]:
    """Return offers awaiting company research (or all, when ``redo``).

    Same idempotency shape as ``select_jobs_to_score``: passed/needs_review rows
    (the scorer judges the needs_review tail too, so both deserve a brief) that
    haven't been researched yet (``company_researched_at IS NULL``). Company
    research runs on *every* such offer (unlike address research, which only
    touches the unroutable tail). ``redo=True`` selects all so a re-run can
    refresh briefs. Rows carry every column for ``JobRecord.from_row``.
    """
    sql = "SELECT * FROM jobs WHERE filter_status IN ('passed', 'needs_review')"
    if not redo:
        sql += " AND company_researched_at IS NULL"
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def record_company_brief(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    company_brief_json: str | None,
) -> None:
    """Persist one offer's company brief (JSON) and stamp ``company_researched_at``.

    A plain UPDATE stamping the timestamp so the row drops out of
    ``select_jobs_to_research_company`` next run (idempotency) and ``--redo``
    overwrites cleanly. ``company_brief_json`` is NULL when research produced
    nothing usable (the row is still stamped so we don't re-attempt every run;
    ``--redo`` retries). The brief is advisory context for the scorer — never a
    hard gate. The caller commits (one commit per batch).
    """
    conn.execute(
        "UPDATE jobs SET company_brief = ?, company_researched_at = ? WHERE id = ?",
        (company_brief_json, _utcnow(), job_id),
    )


def select_jobs_to_research_address(
    conn: sqlite3.Connection, *, limit: int | None = None, redo: bool = False
) -> list[sqlite3.Row]:
    """Return candidate rows for the address agent (the unroutable tail).

    The agent's true worklist — "is this address too vague to route?" — is
    decided by ``enrich.address.resolve_address`` at run time, NOT by SQL, so
    ``resolve_address`` stays the single authority on address quality. This
    helper just narrows to the plausible candidates cheaply: passed/needs_review
    rows the agent hasn't already placed (``address_source`` is not ``'agent'``).
    The stage then calls ``resolve_address`` per row and only invokes the agent
    when it returns ``needs_address``/``unresolved``. ``redo=True`` also re-offers
    rows already placed by the agent (so a better search can be re-attempted).

    Deliberately does NOT gate on ``enriched_at``: research runs *before*
    ``enrich-commute`` in the pipeline (which is the sole router), so at this
    point the tail carries no enrichment stamp yet.
    """
    sql = "SELECT * FROM jobs WHERE filter_status IN ('passed', 'needs_review')"
    if not redo:
        sql += " AND (address_source IS NULL OR address_source != 'agent')"
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def record_agent_address(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    address: str,
    lat: float,
    lon: float,
    address_confidence: str,
) -> None:
    """Persist an address the research agent found and validation confirmed.

    Writes the office address + coordinates with ``address_source='agent'`` and
    **clears any stale commute + enrichment stamp** (``commute_minutes``,
    ``commute_mode``, ``commute_strategies``, ``enriched_at`` → NULL). Routing is
    owned entirely by ``enrich-commute``, which runs after research; nulling the
    stamp is what makes it re-route this row on its *normal* pass rather than
    skipping it as already-enriched. This matters when the agent sharpens an
    address on a row enriched in a PRIOR run from a worse address — otherwise the
    old (now-wrong) commute would linger and never be recomputed without
    ``--re-enrich``. The candidate has already passed the deterministic
    BAN-geocode + IDF validation net in the caller (a wrong address can never
    hard-reject an offer). The caller commits.
    """
    conn.execute(
        "UPDATE jobs SET address = ?, lat = ?, lon = ?, address_source = 'agent', "
        "address_confidence = ?, commute_minutes = NULL, commute_mode = NULL, "
        "commute_strategies = NULL, enriched_at = NULL WHERE id = ?",
        (address, lat, lon, address_confidence, job_id),
    )

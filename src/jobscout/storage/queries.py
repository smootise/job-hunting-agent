"""Read/display queries: the counterpart to ``db.py``'s writes and worklists.

``db.py`` owns everything the *pipeline* needs — upserts, idempotency, and the
``select_jobs_to_*`` worklists (each gated on a ``*_at IS NULL`` predicate so a
stage only picks up newly-eligible offers). None of that helps a *reader* who
just wants "show me every offer, ranked" or "give me offer 42". This module is
that reader's layer, and it exists so the webapp never reaches into the schema
directly.

Two ideas do the work here:

1. **Sorting is guarded.** The offer list can sort by a fixed set of columns
   *or* by any scoring criterion — and a criterion name arrives from an HTTP
   query string, i.e. untrusted. We never interpolate a raw sort string into
   SQL. Plain columns are checked against ``ALLOWED_SORT_COLUMNS``; a criterion
   name is checked against the rubric loaded from ``preferences.yaml`` before its
   JSON path is built server-side. Anything else raises ``ValueError``.

2. **JSON columns are decoded once, defensively.** ``score_json``,
   ``company_brief``, ``commute_strategies`` and ``filter_reasons`` are stored as
   TEXT (see ``db.py``). They can be NULL (unscored / unresearched rows) and, in
   at least one live row, contain mojibake. ``parse_json_column`` swallows both
   so a single bad row never 500s the list, and ``hydrate_job`` pre-parses them
   so templates stay dumb (no ``json.loads`` in Jinja).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# Plain columns a caller may sort by. Deliberately a small allowlist rather than
# "any column": the value is formatted into the SQL ``ORDER BY`` (SQLite can't
# bind an identifier), so it MUST NOT be attacker-controlled. Anything outside
# this set — or the ``criteria:<name>`` form handled below — is rejected.
ALLOWED_SORT_COLUMNS: frozenset[str] = frozenset(
    {
        "score_total",
        "commute_minutes",
        "posted_at",
        "first_seen_at",
        "company",
        "title",
        "filter_status",
    }
)

# The Python-computed commute criterion lives at the TOP level of score_json
# (``$.weekly_commute_fit``), NOT inside ``criteria_scores`` (which holds only
# the 10 LLM-scored criteria). It's a real rubric criterion the owner may want
# to sort by, so it needs its own JSON path. See docs/scoring.md.
_COMMUTE_CRITERION = "weekly_commute_fit"

# Sentinel disposition value meaning "offers the owner hasn't reviewed yet" — i.e.
# no ``offer_review`` row. A real disposition string filters on equality; this
# filters on ``r.disposition IS NULL`` (the LEFT JOIN miss). A named constant
# rather than an empty string so routes/tests reference it unambiguously.
UNREVIEWED = "__unreviewed__"

# The base FROM for the display queries: every job, LEFT JOINed to its optional
# review row. LEFT (not INNER) so unreviewed offers still appear, with NULL
# review columns. ``jobs.*`` keeps every jobs column (so ``hydrate_job`` still
# finds the JSON blobs); the three ``r.*`` columns ride along for the UI.
_JOBS_WITH_REVIEW = (
    "SELECT jobs.*, r.disposition, r.notes, r.applied_at "
    "FROM jobs LEFT JOIN offer_review r ON r.job_id = jobs.id"
)


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """Return the full row for one offer, or None if no such id.

    LEFT JOINs ``offer_review`` so the caller (``hydrate_job``) gets every jobs
    column (incl. the JSON blobs the detail page renders) plus the owner's review
    state; a NULL review is normal (an unreviewed offer).
    """
    return conn.execute(
        f"{_JOBS_WITH_REVIEW} WHERE jobs.id = ?", (job_id,)
    ).fetchone()


def _order_by_clause(order_by: str, descending: bool, criteria_names: frozenset[str]) -> str:
    """Build a validated ``ORDER BY`` clause, or raise ``ValueError``.

    Two accepted forms:
      * a plain column in ``ALLOWED_SORT_COLUMNS`` (e.g. ``"score_total"``); and
      * ``"criteria:<name>"``, where ``<name>`` is a rubric criterion — resolved
        to ``json_extract(score_json, '$.criteria_scores.<name>')`` (or the
        top-level path for ``weekly_commute_fit``).

    In both cases the sort key ends up NULL for rows that were never scored
    (``score_total`` is NULL on needs_review; ``json_extract`` returns NULL when
    the key is absent). Those must sink to the bottom regardless of direction,
    so we prepend ``<expr> IS NULL`` — SQLite sorts False(0) before True(1),
    putting non-NULLs first. This is the NULLS-LAST idiom SQLite lacks natively.

    Exprs are qualified with ``jobs.`` because the display queries LEFT JOIN
    ``offer_review``; qualifying is harmless without the join (``jobs`` = the
    table) and unambiguous with it.
    """
    direction = "DESC" if descending else "ASC"

    if order_by.startswith("criteria:"):
        name = order_by.split(":", 1)[1]
        if name not in criteria_names:
            raise ValueError(f"unknown scoring criterion: {name!r}")
        # `name` is now known-safe (it came from the rubric, not the request).
        if name == _COMMUTE_CRITERION:
            path = "$.weekly_commute_fit"
        else:
            path = f"$.criteria_scores.{name}"
        expr = f"json_extract(jobs.score_json, '{path}')"
    elif order_by in ALLOWED_SORT_COLUMNS:
        expr = f"jobs.{order_by}"
    else:
        raise ValueError(f"invalid sort column: {order_by!r}")

    return f"ORDER BY {expr} IS NULL, {expr} {direction}, jobs.id {direction}"


def list_jobs(
    conn: sqlite3.Connection,
    *,
    filter_status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    source: str | None = None,
    score_status: str | None = None,
    disposition: str | None = None,
    posted_after: str | None = None,
    include_undated: bool = True,
    order_by: str = "score_total",
    descending: bool = True,
    limit: int | None = None,
    offset: int = 0,
    criteria_names: frozenset[str] | None = None,
) -> list[sqlite3.Row]:
    """Return offers for the list view, filtered and sorted.

    Filters (``filter_status``, ``source``, ``score_status``, ``disposition``) are
    exact-match and parameterized; a None filter is simply omitted. ``disposition``
    also accepts the ``UNREVIEWED`` sentinel → offers with no review row.

    ``statuses`` is a multi-value alternative to ``filter_status`` (``IN (...)``),
    used for the "active only" default view (passed + needs_review). Pass one or
    the other, not both.

    ``posted_after`` (an ISO date string, e.g. ``"2026-07-07"``) keeps offers with
    ``posted_at >= posted_after``. Because LinkedIn offers carry **no** ``posted_at``
    (it's NULL for ~1/3 of the table), ``include_undated`` (default True) decides
    their fate: True keeps undated offers regardless of the date bound (never
    silently dropped — the project's core rule); False excludes them. Comparison is
    lexical over ISO-8601 strings, which is correct for date ordering; the caller
    (route) is responsible for validating the date's shape.

    ``order_by`` accepts a plain allowlisted column or the ``criteria:<name>`` form
    (see ``_order_by_clause``); ``criteria_names`` is the set of valid rubric
    criteria — required only when sorting by a criterion.

    The base query LEFT JOINs ``offer_review`` (``_JOBS_WITH_REVIEW``), so jobs
    columns are qualified ``jobs.`` and review columns ``r.``. Sorting/paging
    happen in SQLite (``json_extract`` in ``ORDER BY``) rather than in Python, so
    a large table doesn't have to be fully materialized.
    """
    where: list[str] = []
    params: list[Any] = []
    if filter_status is not None:
        where.append("jobs.filter_status = ?")
        params.append(filter_status)
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        where.append(f"jobs.filter_status IN ({placeholders})")
        params.extend(statuses)
    if posted_after is not None:
        if include_undated:
            # Keep dated offers on/after the bound, PLUS all undated (NULL) offers —
            # never silently drop the ~1/3 of the table (LinkedIn) with no post date.
            where.append("(jobs.posted_at >= ? OR jobs.posted_at IS NULL)")
        else:
            where.append("jobs.posted_at >= ?")
        params.append(posted_after)
    if source is not None:
        where.append("jobs.source = ?")
        params.append(source)
    if score_status is not None:
        where.append("jobs.score_status = ?")
        params.append(score_status)
    if disposition == UNREVIEWED:
        where.append("r.disposition IS NULL")  # no review row = unreviewed
    elif disposition is not None:
        where.append("r.disposition = ?")
        params.append(disposition)

    sql = _JOBS_WITH_REVIEW
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " " + _order_by_clause(order_by, descending, criteria_names or frozenset())

    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    return conn.execute(sql, params).fetchall()


def parse_json_column(value: str | None, default: Any) -> Any:
    """Decode a TEXT JSON column, returning ``default`` on NULL or invalid JSON.

    The single choke point for the "stored JSON might be NULL or corrupt" reality
    (unscored rows are NULL; one live row has mojibake). Returning the caller's
    ``default`` (``{}`` or ``[]``) keeps one bad row from breaking a whole list
    render — the row just shows as un-scored/un-researched rather than 500ing.
    """
    if value is None:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def hydrate_job(row: sqlite3.Row) -> dict[str, Any]:
    """Turn a raw row into a template-ready dict with JSON columns pre-parsed.

    Templates should never call ``json.loads``; they read ``job["score"]`` etc.
    Missing/invalid blobs become empty dict/list so ``{% if job.score %}`` and
    ``job.company_brief.summary`` are always safe.
    """
    data = dict(row)
    data["score"] = parse_json_column(data.get("score_json"), {})
    data["company_brief"] = parse_json_column(data.get("company_brief"), {})
    data["commute"] = parse_json_column(data.get("commute_strategies"), {})
    data["filter_reasons"] = parse_json_column(data.get("filter_reasons"), [])
    # The owner's review state (from the LEFT JOIN; all None for an unreviewed
    # offer). Nested so templates read ``job.review.disposition``.
    data["review"] = {
        "disposition": data.get("disposition"),
        "notes": data.get("notes"),
        "applied_at": data.get("applied_at"),
    }
    return data

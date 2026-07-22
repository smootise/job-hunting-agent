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


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """Return the full row for one offer, or None if no such id.

    A plain ``SELECT *`` so the caller (``hydrate_job``) gets every column,
    including the JSON blobs the detail page renders.
    """
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


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
        expr = f"json_extract(score_json, '{path}')"
    elif order_by in ALLOWED_SORT_COLUMNS:
        expr = order_by
    else:
        raise ValueError(f"invalid sort column: {order_by!r}")

    return f"ORDER BY {expr} IS NULL, {expr} {direction}, id {direction}"


def list_jobs(
    conn: sqlite3.Connection,
    *,
    filter_status: str | None = None,
    source: str | None = None,
    score_status: str | None = None,
    order_by: str = "score_total",
    descending: bool = True,
    limit: int | None = None,
    offset: int = 0,
    criteria_names: frozenset[str] | None = None,
) -> list[sqlite3.Row]:
    """Return offers for the list view, filtered and sorted.

    Filters (``filter_status``, ``source``, ``score_status``) are exact-match and
    parameterized; a None filter is simply omitted. ``order_by`` accepts a plain
    allowlisted column or the ``criteria:<name>`` form (see ``_order_by_clause``);
    ``criteria_names`` is the set of valid rubric criteria (from the caller's
    cached ``preferences.yaml``) — required only when sorting by a criterion.

    Sorting/paging happen in SQLite (``json_extract`` in ``ORDER BY``) rather than
    in Python, so a large table doesn't have to be fully materialized. On an
    exotic SQLite lacking the JSON1 extension you'd fall back to sorting the
    parsed rows in Python; the bundled Python 3.12 SQLite (3.40+) has JSON1, so
    that path isn't needed here.
    """
    where: list[str] = []
    params: list[Any] = []
    if filter_status is not None:
        where.append("filter_status = ?")
        params.append(filter_status)
    if source is not None:
        where.append("source = ?")
        params.append(source)
    if score_status is not None:
        where.append("score_status = ?")
        params.append(score_status)

    sql = "SELECT * FROM jobs"
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
    return data

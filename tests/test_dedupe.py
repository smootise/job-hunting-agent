"""Tests for storage idempotency and cross-source dedupe.

This is the heart of Phase 1's learning goal. The invariants under test:
  1. Re-upserting the same (source, external_id) inserts once and only bumps
     last_seen_at (idempotency — a re-run must not create duplicate rows).
  2. The same logical job from two different sources shares a dup_group.
  3. Normalized matching does NOT falsely merge unrelated jobs (the CLAUDE.md
     substring/whole-token trap: "PM" must not collide with "development").
"""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.models import JobRecord
from jobscout.storage import db


@pytest.fixture
def conn() -> sqlite3.Connection:
    """An in-memory DB with the schema — no filesystem, fast, isolated."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    yield c
    c.close()


def _job(source: str, external_id: str, title: str, company: str) -> JobRecord:
    return JobRecord(
        source=source, external_id=external_id, url=f"https://x/{external_id}",
        title=title, company=company, location="Paris", contract_type="CDI",
        salary_text=None, description=None, posted_at=None, lang="fr",
    )


def test_reupsert_is_idempotent(conn):
    """The core idempotency guarantee: same batch twice -> one row."""
    batch = [_job("wttj", "abc", "Product Manager", "Acme")]

    first = db.upsert_jobs(conn, batch)
    assert first.inserted == 1 and first.seen_again == 0

    second = db.upsert_jobs(conn, batch)
    assert second.inserted == 0 and second.seen_again == 1

    count = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert count == 1


def test_reupsert_bumps_last_seen_but_not_first_seen(conn):
    batch = [_job("wttj", "abc", "Product Manager", "Acme")]
    db.upsert_jobs(conn, batch)
    row1 = conn.execute("SELECT first_seen_at, last_seen_at FROM jobs").fetchone()

    db.upsert_jobs(conn, batch)
    row2 = conn.execute("SELECT first_seen_at, last_seen_at FROM jobs").fetchone()

    assert row2["first_seen_at"] == row1["first_seen_at"]  # unchanged
    assert row2["last_seen_at"] >= row1["last_seen_at"]  # bumped (or equal)


def test_cross_source_same_job_shares_dup_group(conn):
    """Same company+title on WTTJ and LinkedIn -> one shared dup_group."""
    db.upsert_jobs(conn, [_job("wttj", "w1", "Product Manager H/F", "Acme SAS")])
    db.upsert_jobs(conn, [_job("linkedin_email", "l1", "Product Manager", "Acme")])

    groups = [r["dup_group"] for r in
              conn.execute("SELECT dup_group FROM jobs ORDER BY id")]
    assert groups[0] is not None
    assert groups[0] == groups[1]  # both grouped together


def test_distinct_jobs_do_not_merge(conn):
    """The whole-token trap: 'PM' in one title must not merge with an
    unrelated 'development' role at the same company."""
    db.upsert_jobs(conn, [_job("wttj", "w1", "Senior PM", "Acme")])
    db.upsert_jobs(conn, [_job("wttj", "w2", "Software Development Engineer", "Acme")])

    rows = conn.execute("SELECT dup_group FROM jobs ORDER BY id").fetchall()
    # Different normalized titles => not the same group (either both None, or
    # distinct); the essential assertion is they are NOT equal-and-shared.
    g0, g1 = rows[0]["dup_group"], rows[1]["dup_group"]
    assert not (g0 is not None and g0 == g1)


def test_intra_source_different_ids_both_inserted(conn):
    result = db.upsert_jobs(conn, [
        _job("wttj", "w1", "Product Manager", "Acme"),
        _job("wttj", "w2", "Product Owner", "Globex"),
    ])
    assert result.inserted == 2

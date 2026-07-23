"""Tests for webapp V2 application tracking: the offer_review write layer.

Offline, against a temp DB seeded like the pipeline leaves it. Covers the schema
migration (offer_review created idempotently on an old DB), upsert_review (incl.
the preserve-original-applied_at rule), review_stats, and list_jobs's disposition
filter + the join not breaking sort/hydrate.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from jobscout.models import JobRecord
from jobscout.storage import db, queries, stats


def _job(ext, **kw):
    d = dict(
        source="wttj", external_id=ext, url=f"http://x/{ext}", title="PM",
        company="ACME", location="Paris", contract_type="CDI", salary_text=None,
        description="d", posted_at=None, lang="fr",
    )
    d.update(kw)
    return JobRecord(**d)


def _seed(db_path, jobs):
    conn = db.connect(db_path)
    db.upsert_jobs(conn, jobs)
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM jobs")}
    for j in jobs:
        db.record_filter_verdict(conn, ids[j.external_id], filter_status="passed", filter_reasons_json="[]")
    conn.commit()
    return conn, ids


# --- schema migration -----------------------------------------------------


def test_offer_review_created_on_fresh_init(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "offer_review" in tables


def test_offer_review_migration_on_old_db(tmp_path):
    # Simulate a pre-V2 DB: jobs + runs but NO offer_review.
    p = tmp_path / "old.db"
    raw = sqlite3.connect(p)
    raw.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, external_id TEXT, "
        "url TEXT, title TEXT, company TEXT, normalized_company TEXT, "
        "normalized_title TEXT, first_seen_at TEXT, last_seen_at TEXT, "
        "UNIQUE(source, external_id));"
    )
    raw.commit()
    assert "offer_review" not in {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    raw.close()

    conn = db.connect(p)  # runs init_schema
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "offer_review" in tables
    db.init_schema(conn)  # idempotent, no error


# --- upsert_review --------------------------------------------------------


def test_upsert_review_lifecycle_preserves_applied_at(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    jid = ids["a"]

    db.upsert_review(conn, jid, disposition="to_review")
    conn.commit()
    row = conn.execute("SELECT * FROM offer_review WHERE job_id=?", (jid,)).fetchone()
    assert row["disposition"] == "to_review" and row["applied_at"] is None

    db.upsert_review(conn, jid, disposition="applied")
    conn.commit()
    applied_1 = conn.execute("SELECT applied_at FROM offer_review WHERE job_id=?", (jid,)).fetchone()["applied_at"]
    assert applied_1 is not None

    # A later notes edit while still applied must NOT move applied_at.
    time.sleep(0.01)
    db.upsert_review(conn, jid, disposition="applied", notes="ping recruiter")
    conn.commit()
    row = conn.execute("SELECT applied_at, notes FROM offer_review WHERE job_id=?", (jid,)).fetchone()
    assert row["applied_at"] == applied_1
    assert row["notes"] == "ping recruiter"

    # Moving away from applied clears applied_at.
    db.upsert_review(conn, jid, disposition="not_interested")
    conn.commit()
    assert conn.execute("SELECT applied_at FROM offer_review WHERE job_id=?", (jid,)).fetchone()["applied_at"] is None

    # A single row throughout (upsert, not duplicate insert).
    assert conn.execute("SELECT COUNT(*) FROM offer_review WHERE job_id=?", (jid,)).fetchone()[0] == 1


def test_upsert_review_bad_disposition_raises(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    with pytest.raises(ValueError):
        db.upsert_review(conn, ids["a"], disposition="bogus")


def test_upsert_review_caller_commits(tmp_path):
    # The helper must NOT commit — a rollback should discard the write.
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    db.upsert_review(conn, ids["a"], disposition="applied")
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM offer_review").fetchone()[0] == 0


# --- review_stats ---------------------------------------------------------


def test_review_stats(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a"), _job("b"), _job("c"), _job("d")])
    db.upsert_review(conn, ids["a"], disposition="applied")
    db.upsert_review(conn, ids["b"], disposition="to_review")
    db.upsert_review(conn, ids["c"], disposition="not_interested")
    conn.commit()  # d left unreviewed
    s = stats.review_stats(conn)
    assert (s.applied, s.to_review, s.not_interested, s.unreviewed) == (1, 1, 1, 1)


def test_review_stats_empty(tmp_path):
    conn, _ = _seed(tmp_path / "j.db", [_job("a")])
    s = stats.review_stats(conn)
    assert (s.applied, s.to_review, s.not_interested, s.unreviewed) == (0, 0, 0, 1)


# --- list_jobs disposition filter + join safety ---------------------------


def test_list_jobs_disposition_filter(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a"), _job("b")])
    db.upsert_review(conn, ids["a"], disposition="applied")
    conn.commit()

    applied = queries.list_jobs(conn, disposition="applied")
    assert [r["external_id"] for r in applied] == ["a"]

    unreviewed = queries.list_jobs(conn, disposition=queries.UNREVIEWED)
    assert [r["external_id"] for r in unreviewed] == ["b"]

    assert len(queries.list_jobs(conn, disposition=None)) == 2


def test_join_surfaces_review_in_hydrate(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    db.upsert_review(conn, ids["a"], disposition="applied", notes="n")
    conn.commit()
    row = queries.get_job(conn, ids["a"])
    h = queries.hydrate_job(row)
    assert h["review"]["disposition"] == "applied"
    assert h["review"]["notes"] == "n"
    assert h["review"]["applied_at"] is not None


def test_sort_still_works_with_join(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a"), _job("b")])
    db.record_score(conn, ids["a"], score_total=90.0, score_status="scored", score_json="{}")
    db.record_score(conn, ids["b"], score_total=50.0, score_status="scored", score_json="{}")
    db.upsert_review(conn, ids["a"], disposition="applied")
    conn.commit()
    rows = queries.list_jobs(conn, order_by="score_total", descending=True)
    assert [r["external_id"] for r in rows] == ["a", "b"]

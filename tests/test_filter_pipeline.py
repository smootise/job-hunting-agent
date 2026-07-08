"""Tests for the hard-filter stage's DB integration and orchestration.

Guards the Phase 2 storage invariants that mirror Phase 1's: verdicts persist,
re-runs are idempotent (judge only un-filtered offers), ``--refilter`` re-judges
all, ``--dry-run`` writes nothing, the schema migration backfills the new
columns on an old DB, and one malformed row can't sink the batch.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jobscout.models import JobRecord
from jobscout.pipeline import filter_stage
from jobscout.storage import db

# A realistic preferences.yaml subset written to a temp file per test.
PREFS_YAML = """\
hard_filters:
  contract_types: ["CDI"]
  salary_floor_eur: 50000
  seniority:
    include_keywords: ["product manager", "product owner", "PM", "PO"]
    exclude_keywords: ["junior", "intern", "stage"]
  company_blocklist: []
  remote_policy:
    accept_onsite: true
    accept_hybrid: true
    accept_remote: true
    min_remote_days_per_week: 0
"""


@pytest.fixture
def prefs_path(tmp_path):
    p = tmp_path / "preferences.yaml"
    p.write_text(PREFS_YAML, encoding="utf-8")
    return p


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "jobs.db"


def _job(source, external_id, title, company, **kw) -> JobRecord:
    return JobRecord(
        source=source, external_id=external_id, url=f"https://x/{external_id}",
        title=title, company=company, location=kw.get("location", "Paris"),
        contract_type=kw.get("contract_type", "CDI"),
        salary_text=kw.get("salary_text"), description=kw.get("description"),
        posted_at=None, lang="en",
    )


def _seed(db_path, jobs):
    conn = db.connect(db_path)
    db.upsert_jobs(conn, jobs)
    conn.close()


# --------------------------------------------------------------------------
# Verdict persistence
# --------------------------------------------------------------------------


def test_verdict_persisted_with_status_reasons_timestamp(db_path, prefs_path):
    _seed(db_path, [
        _job("wttj", "1", "Senior Product Manager", "Acme",
             location="Full remote", salary_text="55-60k"),   # clean pass
        _job("wttj", "2", "Product Manager", "Acme", contract_type="CDD"),  # reject
        _job("wttj", "3", "Product Manager", "Acme", contract_type=None),   # needs_review
    ])
    summary = filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path)
    assert (summary.passed, summary.rejected) == (1, 1)
    assert summary.needs_review == 1

    conn = db.connect(db_path)
    rows = {r["external_id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["1"]["filter_status"] == "passed"
    assert rows["2"]["filter_status"] == "rejected"
    assert rows["3"]["filter_status"] == "needs_review"
    # reasons is valid JSON; a clean pass has an empty list.
    assert json.loads(rows["1"]["filter_reasons"]) == []
    assert any(x["filter"] == "contract_type" for x in json.loads(rows["2"]["filter_reasons"]))
    assert rows["2"]["filtered_at"] is not None
    conn.close()


# --------------------------------------------------------------------------
# Idempotency + refilter + dry-run
# --------------------------------------------------------------------------


def test_rerun_is_idempotent(db_path, prefs_path):
    _seed(db_path, [_job("wttj", "1", "Product Manager", "Acme", contract_type="CDD")])
    first = filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path)
    assert first.total == 1
    # Second run: nothing left un-filtered.
    second = filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path)
    assert second.total == 0


def test_refilter_rejudges_all(db_path, prefs_path):
    _seed(db_path, [_job("wttj", "1", "Product Manager", "Acme", contract_type="CDD")])
    filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path)
    again = filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path, refilter=True)
    assert again.total == 1 and again.rejected == 1


def test_dry_run_writes_nothing_but_counts(db_path, prefs_path):
    _seed(db_path, [_job("wttj", "1", "Product Manager", "Acme", contract_type="CDD")])
    summary = filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path, dry_run=True)
    assert summary.total == 1 and summary.rejected == 1

    conn = db.connect(db_path)
    status = conn.execute("SELECT filter_status FROM jobs WHERE external_id='1'").fetchone()
    assert status["filter_status"] is None  # untouched — dry-run wrote nothing
    conn.close()


def test_limit_caps_offers_judged(db_path, prefs_path):
    _seed(db_path, [
        _job("wttj", "1", "Product Manager", "Acme", contract_type="CDD"),
        _job("wttj", "2", "Product Owner", "Globex", contract_type="CDD"),
    ])
    summary = filter_stage.run_filter(prefs_path=prefs_path, db_path=db_path, limit=1)
    assert summary.total == 1


# --------------------------------------------------------------------------
# Schema migration (Phase 1 DB → Phase 2 columns)
# --------------------------------------------------------------------------


def test_new_columns_present_after_fresh_init():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"filter_status", "filter_reasons", "filtered_at"} <= cols
    conn.close()


def test_migration_backfills_columns_on_old_schema():
    # Simulate a Phase 1 `jobs` table without the Phase 2 columns.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, external_id TEXT, "
        "url TEXT, title TEXT, company TEXT, location TEXT, contract_type TEXT, "
        "salary_text TEXT, description TEXT, posted_at TEXT, lang TEXT, "
        "normalized_company TEXT, normalized_title TEXT, dup_group INTEGER, "
        "status TEXT, first_seen_at TEXT, last_seen_at TEXT)"
    )
    conn.commit()
    before = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "filter_status" not in before

    db.init_schema(conn)  # should ALTER-add the missing columns

    after = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"filter_status", "filter_reasons", "filtered_at"} <= after
    # Idempotent: running again doesn't error.
    db.init_schema(conn)
    conn.close()


# --------------------------------------------------------------------------
# Fail-soft
# --------------------------------------------------------------------------


def test_judge_row_failsoft_records_needs_review():
    # Directly exercise the per-row except path: a row that raises when read
    # must become needs_review with an error reason, never propagate.
    class Boom(dict):
        def __getitem__(self, key):
            raise RuntimeError("boom")

    verdict = filter_stage._judge_row(Boom(), {"hard_filters": {}})
    assert verdict.outcome.value == "needs_review"
    assert any(r.filter == "error" for r in verdict.reasons)

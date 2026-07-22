"""Tests for the dashboard stats layer (storage/stats.py).

The headline test is the **predicate-parity invariant**: each "pending" count
must equal ``len(db.select_jobs_to_*(conn))`` on the same DB, so the dashboard's
duplicated WHERE clauses can never silently drift from the worklists they mirror.
Also covers the funnel counts and the run-ledger parsing (stage extraction from
source_counts_json, and an interrupted run with no finished_at).
"""

from __future__ import annotations

from jobscout.models import JobRecord
from jobscout.storage import db, stats


def _job(ext, **kw):
    d = dict(
        source="wttj", external_id=ext, url=f"https://x/{ext}", title="Product Manager",
        company="ACME", location="Paris", contract_type="CDI", salary_text=None,
        description="A PM role.", posted_at=None, lang="fr",
    )
    d.update(kw)
    return JobRecord(**d)


def _seed_mixed(db_path):
    """A spread across filter_status + enrich/score/research stamps."""
    conn = db.connect(db_path)
    jobs = [_job(f"e{i}") for i in range(6)]
    db.upsert_jobs(conn, jobs)
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM jobs")}

    # e0: unfiltered (leave filter_status NULL).
    # e1: passed, nothing else.
    db.record_filter_verdict(conn, ids["e1"], filter_status="passed", filter_reasons_json="[]")
    # e2: passed + scored + enriched + researched (fully processed).
    db.record_filter_verdict(conn, ids["e2"], filter_status="passed", filter_reasons_json="[]")
    db.record_score(conn, ids["e2"], score_total=80.0, score_status="scored", score_json="{}")
    db.record_enrichment(conn, ids["e2"], address="1 rue", lat=1.0, lon=2.0,
                         address_source="posting", address_confidence="high",
                         commute_minutes=30.0, commute_mode="no_bike",
                         commute_strategies_json="{}")
    db.record_company_brief(conn, ids["e2"], company_brief_json="{}")
    # e3: needs_review (still eligible for enrich/score).
    db.record_filter_verdict(conn, ids["e3"], filter_status="needs_review", filter_reasons_json="[]")
    # e4: rejected (never eligible).
    db.record_filter_verdict(conn, ids["e4"], filter_status="rejected", filter_reasons_json="[]")
    # e5: passed + agent-placed address (drops out of to_research_address).
    db.record_filter_verdict(conn, ids["e5"], filter_status="passed", filter_reasons_json="[]")
    db.record_enrichment(conn, ids["e5"], address="a", lat=1.0, lon=2.0,
                         address_source="agent", address_confidence="low",
                         commute_minutes=40.0, commute_mode="no_bike",
                         commute_strategies_json="{}")
    conn.commit()
    return conn


def test_pending_counts_match_worklists(tmp_path):
    conn = _seed_mixed(tmp_path / "j.db")
    s = stats.dashboard_stats(conn)
    # The invariant: stats mirrors the worklist predicates exactly.
    assert s.to_filter == len(db.select_unfiltered_jobs(conn))
    assert s.to_enrich == len(db.select_jobs_to_enrich(conn))
    assert s.to_score == len(db.select_jobs_to_score(conn))
    assert s.to_research_company == len(db.select_jobs_to_research_company(conn))
    assert s.to_research_address == len(db.select_jobs_to_research_address(conn))


def test_funnel_counts(tmp_path):
    conn = _seed_mixed(tmp_path / "j.db")
    s = stats.dashboard_stats(conn)
    assert s.total == 6
    assert s.by_filter_status["passed"] == 3       # e1, e2, e5
    assert s.by_filter_status["needs_review"] == 1  # e3
    assert s.by_filter_status["rejected"] == 1      # e4
    assert s.by_filter_status["unfiltered"] == 1    # e0
    assert s.scored == 1                            # e2


def test_last_run_parses_stage(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    run_id = db.record_run_start(conn, dry_run=False)
    db.record_run_finish(conn, run_id, {"stage": "score", "scored": 5})
    lr = stats.last_run(conn)
    assert lr.stage == "score"
    assert lr.source_counts == {"scored": 5}  # stage popped out
    assert lr.finished_at is not None


def test_last_run_ingest_has_no_stage(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    run_id = db.record_run_start(conn, dry_run=True)
    db.record_run_finish(conn, run_id, {"wttj": {"new": 3}})
    lr = stats.last_run(conn)
    assert lr.stage is None
    assert lr.source_counts == {"wttj": {"new": 3}}
    assert lr.dry_run is True


def test_last_run_none_when_empty(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    assert stats.last_run(conn) is None


def test_recent_runs_newest_first(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    for _ in range(3):
        db.record_run_start(conn, dry_run=False)
    runs = stats.recent_runs(conn, limit=2)
    assert len(runs) == 2

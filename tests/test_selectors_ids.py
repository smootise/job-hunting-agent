"""Tests for the per-offer ``ids`` targeting on the worklist selectors.

The webapp's per-offer buttons re-run a single stage on one offer, which means
each ``select_jobs_to_*`` must, given ``ids``, (a) restrict to those ids, (b) drop
its 'already done' idempotency gate (so a deliberate re-run works), yet (c) keep
its real eligibility gate (a rejected offer is never enriched/researched/scored;
that's a safety rail, not idempotency). These tests pin exactly that.
"""

from __future__ import annotations

from jobscout.models import JobRecord
from jobscout.storage import db


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
    return conn, ids


def test_enrich_ids_bypasses_done_gate_keeps_eligibility(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a"), _job("rej")])
    # 'a' passed AND already enriched; 'rej' rejected.
    db.record_filter_verdict(conn, ids["a"], filter_status="passed", filter_reasons_json="[]")
    db.record_enrichment(conn, ids["a"], address="x", lat=1.0, lon=2.0,
                         address_source="posting", address_confidence="high",
                         commute_minutes=30.0, commute_mode="no_bike", commute_strategies_json="{}")
    db.record_filter_verdict(conn, ids["rej"], filter_status="rejected", filter_reasons_json="[]")
    conn.commit()

    # Default: 'a' is enriched → not selected.
    assert db.select_jobs_to_enrich(conn) == []
    # ids=['a']: re-offered despite enriched_at set (gate dropped).
    got = db.select_jobs_to_enrich(conn, ids=[ids["a"]])
    assert [r["id"] for r in got] == [ids["a"]]
    # ids=[rejected]: eligibility gate still excludes it → empty (safe no-op).
    assert db.select_jobs_to_enrich(conn, ids=[ids["rej"]]) == []


def test_research_company_ids_bypass_and_eligibility(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a"), _job("rej")])
    db.record_filter_verdict(conn, ids["a"], filter_status="passed", filter_reasons_json="[]")
    db.record_company_brief(conn, ids["a"], company_brief_json="{}")  # already researched
    db.record_filter_verdict(conn, ids["rej"], filter_status="rejected", filter_reasons_json="[]")
    conn.commit()

    assert db.select_jobs_to_research_company(conn) == []  # 'a' done
    assert [r["id"] for r in db.select_jobs_to_research_company(conn, ids=[ids["a"]])] == [ids["a"]]
    assert db.select_jobs_to_research_company(conn, ids=[ids["rej"]]) == []


def test_research_address_ids_reoffers_agent_placed(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    db.record_filter_verdict(conn, ids["a"], filter_status="passed", filter_reasons_json="[]")
    db.record_agent_address(conn, ids["a"], address="x", lat=1.0, lon=2.0, address_confidence="medium")
    conn.commit()

    # Default: agent-placed rows are excluded.
    assert db.select_jobs_to_research_address(conn) == []
    # ids: re-offered so the agent can re-search a suspect address.
    assert [r["id"] for r in db.select_jobs_to_research_address(conn, ids=[ids["a"]])] == [ids["a"]]


def test_missing_description_ids_reoffers_and_source_gated(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [
        _job("li", source="linkedin_email", description="already here"),
        _job("wttj_a", source="wttj"),
    ])
    conn.commit()
    # Default (description present) → not selected.
    assert db.select_jobs_missing_description(conn) == []
    # ids: re-offered despite having a description (gate dropped).
    got = db.select_jobs_missing_description(conn, ids=[ids["li"]])
    assert [r["id"] for r in got] == [ids["li"]]
    # Source gate stays: a wttj id with the linkedin source filter → empty.
    assert db.select_jobs_missing_description(conn, ids=[ids["wttj_a"]]) == []


def test_filter_ids_reoffers_rejected(tmp_path):
    # filter has no eligibility gate (it SETS filter_status), so a rejected offer
    # can be re-filtered by id (e.g. to un-reject after a prefs change).
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    db.record_filter_verdict(conn, ids["a"], filter_status="rejected", filter_reasons_json="[]")
    conn.commit()
    assert db.select_unfiltered_jobs(conn) == []  # already judged
    assert [r["id"] for r in db.select_unfiltered_jobs(conn, ids=[ids["a"]])] == [ids["a"]]


def test_empty_ids_selects_nothing(tmp_path):
    conn, ids = _seed(tmp_path / "j.db", [_job("a")])
    db.record_filter_verdict(conn, ids["a"], filter_status="passed", filter_reasons_json="[]")
    conn.commit()
    # An explicit empty id list means 'no offers', never 'all'.
    assert db.select_jobs_to_enrich(conn, ids=[]) == []
    assert db.select_jobs_to_research_company(conn, ids=[]) == []
    assert db.select_unfiltered_jobs(conn, ids=[]) == []

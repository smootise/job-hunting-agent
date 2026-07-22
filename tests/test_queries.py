"""Tests for the read/display layer (storage/queries.py).

Offline, against a temp DB seeded the way the pipeline leaves it: upsert →
filter verdict → score. Covers get_job hit/miss, list_jobs filtering, the two
sort forms (plain column + criteria:<name>) including the weekly_commute_fit
top-level special case, NULLS-LAST for unscored rows, the security guard that
rejects an unknown sort key, and parse_json_column's defensive decoding.
"""

from __future__ import annotations

import json

import pytest

from jobscout.models import JobRecord
from jobscout.storage import db, queries

CRITERIA = frozenset({"product_culture_and_management", "weekly_commute_fit"})


def _job(**kw):
    d = dict(
        source="wttj", external_id="e1", url="https://x/1", title="Product Manager",
        company="ACME", location="Paris", contract_type="CDI", salary_text=None,
        description="A PM role.", posted_at=None, lang="fr",
    )
    d.update(kw)
    return JobRecord(**d)


def _seed(db_path, jobs, *, scores=None):
    """jobs: list of JobRecord. scores: dict external_id -> (total, score_json dict)."""
    conn = db.connect(db_path)
    db.upsert_jobs(conn, jobs)
    for job in jobs:
        row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (job.external_id,)).fetchone()
        db.record_filter_verdict(conn, row["id"], filter_status="passed", filter_reasons_json="[]")
        if scores and job.external_id in scores:
            total, sj = scores[job.external_id]
            db.record_score(conn, row["id"], score_total=total, score_status="scored",
                            score_json=json.dumps(sj))
    conn.commit()
    return conn


def test_get_job_hit_and_miss(tmp_path):
    conn = _seed(tmp_path / "j.db", [_job()])
    row = conn.execute("SELECT id FROM jobs").fetchone()
    assert queries.get_job(conn, row["id"])["title"] == "Product Manager"
    assert queries.get_job(conn, 999999) is None


def test_list_jobs_filter_by_source(tmp_path):
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", source="wttj"),
        _job(external_id="b", source="france_travail"),
    ])
    rows = queries.list_jobs(conn, source="wttj")
    assert len(rows) == 1 and rows[0]["source"] == "wttj"


def test_sort_by_score_total_desc_nulls_last(tmp_path):
    # One scored (70), one scored (90), one unscored (NULL score_total).
    conn = _seed(
        tmp_path / "j.db",
        [_job(external_id="a"), _job(external_id="b"), _job(external_id="c")],
        scores={
            "a": (70.0, {"criteria_scores": {}}),
            "b": (90.0, {"criteria_scores": {}}),
        },
    )
    rows = queries.list_jobs(conn, order_by="score_total", descending=True)
    totals = [r["score_total"] for r in rows]
    assert totals == [90.0, 70.0, None]  # NULL sinks to the bottom even desc


def test_sort_by_criterion(tmp_path):
    conn = _seed(
        tmp_path / "j.db",
        [_job(external_id="a"), _job(external_id="b")],
        scores={
            "a": (70.0, {"criteria_scores": {"product_culture_and_management": 3}}),
            "b": (70.0, {"criteria_scores": {"product_culture_and_management": 9}}),
        },
    )
    rows = queries.list_jobs(
        conn, order_by="criteria:product_culture_and_management",
        descending=True, criteria_names=CRITERIA,
    )
    assert [r["external_id"] for r in rows] == ["b", "a"]


def test_sort_by_weekly_commute_fit_uses_top_level_key(tmp_path):
    # weekly_commute_fit lives at $.weekly_commute_fit, not in criteria_scores.
    conn = _seed(
        tmp_path / "j.db",
        [_job(external_id="a"), _job(external_id="b")],
        scores={
            "a": (70.0, {"weekly_commute_fit": 2.0, "criteria_scores": {}}),
            "b": (70.0, {"weekly_commute_fit": 8.0, "criteria_scores": {}}),
        },
    )
    rows = queries.list_jobs(
        conn, order_by="criteria:weekly_commute_fit",
        descending=True, criteria_names=CRITERIA,
    )
    assert [r["external_id"] for r in rows] == ["b", "a"]


def test_unknown_sort_key_raises(tmp_path):
    conn = _seed(tmp_path / "j.db", [_job()])
    with pytest.raises(ValueError):
        queries.list_jobs(conn, order_by="score_total; DROP TABLE jobs")
    with pytest.raises(ValueError):
        queries.list_jobs(conn, order_by="criteria:not_a_criterion", criteria_names=CRITERIA)


def test_parse_json_column():
    assert queries.parse_json_column(None, {}) == {}
    assert queries.parse_json_column("not json{", []) == []
    assert queries.parse_json_column('{"a": 1}', {}) == {"a": 1}


def test_hydrate_job_parses_blobs(tmp_path):
    conn = _seed(
        tmp_path / "j.db", [_job()],
        scores={"e1": (70.0, {"reasoning": "good fit", "red_flags": ["x"], "criteria_scores": {}})},
    )
    row = queries.get_job(conn, conn.execute("SELECT id FROM jobs").fetchone()["id"])
    data = queries.hydrate_job(row)
    assert data["score"]["reasoning"] == "good fit"
    assert data["filter_reasons"] == []
    assert data["company_brief"] == {}  # NULL blob → empty dict

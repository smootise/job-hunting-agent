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


def test_posted_after_filters_dated_and_keeps_undated_by_default(tmp_path):
    # a: old (2026-01-01), b: recent (2026-08-01), c: undated (NULL posted_at).
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", posted_at="2026-01-01T00:00:00Z"),
        _job(external_id="b", posted_at="2026-08-01T00:00:00Z"),
        _job(external_id="c", posted_at=None),
    ])
    rows = queries.list_jobs(conn, posted_after="2026-07-01")
    ids = {r["external_id"] for r in rows}
    assert ids == {"b", "c"}  # old 'a' dropped; undated 'c' kept (never silently dropped)


def test_posted_after_hide_undated_excludes_null(tmp_path):
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", posted_at="2026-01-01T00:00:00Z"),
        _job(external_id="b", posted_at="2026-08-01T00:00:00Z"),
        _job(external_id="c", posted_at=None),
    ])
    rows = queries.list_jobs(conn, posted_after="2026-07-01", include_undated=False)
    assert {r["external_id"] for r in rows} == {"b"}  # 'c' now excluded too


def test_list_jobs_source_accepts_multiple(tmp_path):
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", source="wttj"),
        _job(external_id="b", source="france_travail"),
        _job(external_id="c", source="linkedin_email"),
    ])
    rows = queries.list_jobs(conn, source=["wttj", "linkedin_email"])
    assert {r["external_id"] for r in rows} == {"a", "c"}


def test_list_jobs_source_single_string_still_works(tmp_path):
    """The one-value form stays valid — callers needn't wrap it in a list."""
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", source="wttj"),
        _job(external_id="b", source="france_travail"),
    ])
    assert len(queries.list_jobs(conn, source="wttj")) == 1


def test_list_jobs_empty_sequence_means_no_filter(tmp_path):
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", source="wttj"),
        _job(external_id="b", source="france_travail"),
    ])
    assert len(queries.list_jobs(conn, source=[])) == 2


def test_list_jobs_multi_filters_are_anded_together(tmp_path):
    """Values inside one filter are OR'd; separate filters are AND'd."""
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a", source="wttj"),
        _job(external_id="b", source="france_travail"),
        _job(external_id="c", source="linkedin_email"),
    ])
    # Score only 'a' and 'b', then ask for (wttj OR linkedin) AND scored.
    for ext in ("a", "b"):
        row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (ext,)).fetchone()
        db.record_score(conn, row["id"], score_total=50.0, score_status="scored",
                        score_json=json.dumps({"criteria_scores": {}}))
    conn.commit()

    rows = queries.list_jobs(
        conn, source=["wttj", "linkedin_email"], score_status=["scored"]
    )
    assert {r["external_id"] for r in rows} == {"a"}  # 'b' wrong source, 'c' unscored


def test_list_jobs_score_status_accepts_multiple(tmp_path):
    conn = _seed(tmp_path / "j.db", [
        _job(external_id="a"), _job(external_id="b"), _job(external_id="c"),
    ])
    marks = {"a": "scored", "b": "needs_review"}
    for ext, status in marks.items():
        row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (ext,)).fetchone()
        db.record_score(conn, row["id"], score_total=50.0, score_status=status,
                        score_json=json.dumps({"criteria_scores": {}}))
    conn.commit()

    rows = queries.list_jobs(conn, score_status=["scored", "needs_review"])
    assert {r["external_id"] for r in rows} == {"a", "b"}  # 'c' never scored


def _set_seen(conn, external_id, ts):
    """Force a row's first_seen_at, simulating an offer from an earlier batch.

    ``upsert_jobs`` stamps 'now' for every insert, so tests that care about
    ingestion *dates* have to rewrite it explicitly.
    """
    conn.execute("UPDATE jobs SET first_seen_at=? WHERE external_id=?", (ts, external_id))
    conn.commit()


def test_seen_after_filters_by_ingestion_date(tmp_path):
    conn = _seed(tmp_path / "j.db", [_job(external_id="old"), _job(external_id="new")])
    _set_seen(conn, "old", "2026-01-01T00:00:00+00:00")
    _set_seen(conn, "new", "2026-08-15T10:00:00+00:00")

    rows = queries.list_jobs(conn, seen_after="2026-08-01")
    assert {r["external_id"] for r in rows} == {"new"}


def test_seen_after_bare_date_includes_same_day_timestamps(tmp_path):
    """A bare YYYY-MM-DD bound must catch offers ingested *during* that day.

    Lexically "2026-08-15" < "2026-08-15T10:00:00+00:00", so the prefix compare
    means "from the start of that day" — the behaviour a date picker implies.
    """
    conn = _seed(tmp_path / "j.db", [_job(external_id="a")])
    _set_seen(conn, "a", "2026-08-15T10:00:00+00:00")
    assert len(queries.list_jobs(conn, seen_after="2026-08-15")) == 1


def test_seen_after_bound_is_inclusive_for_exact_timestamp(tmp_path):
    """Passing a run's started_at selects that run's own intake (>=, not >)."""
    conn = _seed(tmp_path / "j.db", [_job(external_id="a")])
    _set_seen(conn, "a", "2026-08-15T10:00:00+00:00")
    assert len(queries.list_jobs(conn, seen_after="2026-08-15T10:00:00+00:00")) == 1


def _record_run(conn, started_at, counts, *, dry_run=0):
    """Insert a runs-ledger row directly (counts already JSON-encoded or None)."""
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, source_counts_json, dry_run) "
        "VALUES (?, ?, ?, ?)",
        (started_at, started_at, counts, dry_run),
    )
    conn.commit()


def test_last_ingest_run_skips_other_stages(tmp_path):
    """Every stage shares the runs ledger; only ingest rows may be returned."""
    conn = db.connect(tmp_path / "j.db")
    _record_run(conn, "2026-08-01T00:00:00+00:00",
                json.dumps({"stage": "ingest", "wttj": {"new": 5}}))
    # A LATER score run must not be mistaken for the last ingest.
    _record_run(conn, "2026-08-02T00:00:00+00:00",
                json.dumps({"stage": "score", "scored": 12}))

    run = queries.last_ingest_run(conn)
    assert run["started_at"] == "2026-08-01T00:00:00+00:00"
    assert run["new_offers"] == 5


def test_last_ingest_run_treats_absent_stage_as_ingest(tmp_path):
    """Ingest predates the 'stage' key, so legacy rows lack it entirely."""
    conn = db.connect(tmp_path / "j.db")
    _record_run(conn, "2026-08-01T00:00:00+00:00",
                json.dumps({"wttj": {"new": 3}, "france_travail": {"new": 4}}))
    run = queries.last_ingest_run(conn)
    assert run["new_offers"] == 7  # summed across sources


def test_last_ingest_run_ignores_dry_runs(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    _record_run(conn, "2026-08-01T00:00:00+00:00", json.dumps({"stage": "ingest", "w": {"new": 1}}))
    _record_run(conn, "2026-08-05T00:00:00+00:00",
                json.dumps({"stage": "ingest", "w": {"new": 9}}), dry_run=1)
    assert queries.last_ingest_run(conn)["started_at"] == "2026-08-01T00:00:00+00:00"


def test_last_ingest_run_skips_corrupt_blob(tmp_path):
    """A corrupt blob says nothing about the stage, so it must be skipped.

    Falling back to {} would make it look stage-less, i.e. a legacy ingest row —
    which would pin the "new" view to a batch that ingested nothing.
    """
    conn = db.connect(tmp_path / "j.db")
    _record_run(conn, "2026-08-01T00:00:00+00:00", json.dumps({"stage": "ingest", "w": {"new": 2}}))
    _record_run(conn, "2026-08-02T00:00:00+00:00", "{not json at all")
    run = queries.last_ingest_run(conn)
    assert run["started_at"] == "2026-08-01T00:00:00+00:00" and run["new_offers"] == 2


def test_last_ingest_run_accepts_null_blob_as_ingest(tmp_path):
    """A NULL blob (crashed/legacy run) is stage-less, so it counts as ingest."""
    conn = db.connect(tmp_path / "j.db")
    _record_run(conn, "2026-08-01T00:00:00+00:00", None)
    run = queries.last_ingest_run(conn)
    assert run["started_at"] == "2026-08-01T00:00:00+00:00" and run["new_offers"] == 0


def test_last_ingest_run_none_on_empty_ledger(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    assert queries.last_ingest_run(conn) is None


def test_last_ingest_run_counts_zero_when_nothing_new(tmp_path):
    """A run that adds nothing is normal — it must report 0, not be skipped."""
    conn = db.connect(tmp_path / "j.db")
    _record_run(conn, "2026-08-01T00:00:00+00:00",
                json.dumps({"stage": "ingest", "wttj": {"new": 0, "seen_again": 80}}))
    assert queries.last_ingest_run(conn)["new_offers"] == 0


def _seed_statuses(db_path):
    """Seed three offers with distinct filter_status verdicts."""
    conn = db.connect(db_path)
    jobs = [_job(external_id="p"), _job(external_id="n"), _job(external_id="x")]
    db.upsert_jobs(conn, jobs)
    verdicts = {"p": "passed", "n": "needs_review", "x": "rejected"}
    for job in jobs:
        row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (job.external_id,)).fetchone()
        db.record_filter_verdict(conn, row["id"], filter_status=verdicts[job.external_id],
                                 filter_reasons_json="[]")
    conn.commit()
    return conn


def test_statuses_multi_value_hides_rejected(tmp_path):
    conn = _seed_statuses(tmp_path / "j.db")
    rows = queries.list_jobs(conn, statuses=("passed", "needs_review"))
    assert {r["external_id"] for r in rows} == {"p", "n"}  # rejected 'x' hidden


def test_no_status_filter_shows_all(tmp_path):
    conn = _seed_statuses(tmp_path / "j.db")
    rows = queries.list_jobs(conn)  # neither filter_status nor statuses
    assert {r["external_id"] for r in rows} == {"p", "n", "x"}


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

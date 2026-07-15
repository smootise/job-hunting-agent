"""Tests for the LLM-scoring orchestration stage.

Fully offline: the ``generate`` call is injected as a stub returning canned JSON,
so no Ollama and no network. Seeds a temp DB the way ``jobscout filter`` +
``enrich-commute`` would leave it, drives ``run_score``, and asserts on the
persisted rows + the summary.

Covers: a clean score written; idempotency (2nd run scores 0; ``--rescore``
redoes); retry-once-then-needs_review on invalid JSON; per-row fail-soft (a model
error doesn't sink the batch); the unknown-commute red_flag + drop-from-total;
the salary-not-stated red_flag; only passed/needs_review scored (rejected
skipped); enrichment-independence (an unenriched row is still scored); and
``--dry-run`` writing nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from jobscout.models import JobRecord
from jobscout.pipeline import score_stage
from jobscout.storage import db

PREFS_YAML = """\
scoring_rubric:
  ideal_role_description: "An ideal PM role."
  criteria:
    - name: weekly_commute_fit
      weight: 9
      description: commute
    - name: product_culture_and_management
      weight: 9
      description: culture
    - name: ai_ml_focus
      weight: 8
      description: ai
    - name: seniority_match
      weight: 7
      description: seniority
"""

QUAL_NAMES = ["product_culture_and_management", "ai_ml_focus", "seniority_match"]


@dataclass
class FakeGen:
    """Mimics llm_client.GenerationResult's .response for the stub."""

    response: str


def prefs_path(tmp_path):
    p = tmp_path / "preferences.yaml"
    p.write_text(PREFS_YAML, encoding="utf-8")
    return p


def valid_json(**overrides) -> str:
    payload = {
        "criteria_scores": {n: 8 for n in QUAL_NAMES},
        "onsite_days": 3,
        "remote_policy": "hybrid",
        "reasoning": "Good fit.",
        "red_flags": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def stub_generate(response: str):
    """A generate() that always returns the same response."""
    def _gen(model, prompt, *, system=None):
        return FakeGen(response=response)
    return _gen


def sequence_generate(*responses: str):
    """A generate() that returns each response in turn (for retry tests)."""
    calls = {"i": 0}
    def _gen(model, prompt, *, system=None):
        r = responses[min(calls["i"], len(responses) - 1)]
        calls["i"] += 1
        return FakeGen(response=r)
    return _gen


def _job(external_id, *, salary_text="55-65k", source="wttj"):
    return JobRecord(
        source=source, external_id=external_id, url=f"http://x/{external_id}",
        title="Product Manager", company="Acme", location="Paris 11e",
        contract_type="CDI", salary_text=salary_text,
        description="A great PM role.", posted_at=None, lang="en",
    )


def _seed(db_path, jobs, *, status="passed", commute_minutes=25.0, filter_reasons="[]"):
    conn = db.connect(db_path)
    db.upsert_jobs(conn, jobs)
    for j in jobs:
        row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (j.external_id,)).fetchone()
        db.record_filter_verdict(conn, row["id"], filter_status=status, filter_reasons_json=filter_reasons)
        if commute_minutes is not None:
            db.record_enrichment(
                conn, row["id"], address="somewhere", lat=48.8, lon=2.3,
                address_source="posting", address_confidence="high",
                commute_minutes=commute_minutes, commute_mode="no_bike",
                commute_strategies_json="{}",
            )
    conn.commit()
    conn.close()


def _row(db_path, external_id):
    conn = db.connect(db_path)
    r = conn.execute("SELECT * FROM jobs WHERE external_id=?", (external_id,)).fetchone()
    conn.close()
    return r


# --------------------------------------------------------------------------


def test_clean_score_written(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")])
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=stub_generate(valid_json()),
    )
    assert summary.scored == 1
    assert summary.needs_review == 0
    row = _row(db_path, "1")
    assert row["score_status"] == "scored"
    assert row["score_total"] is not None
    blob = json.loads(row["score_json"])
    assert blob["commute_included_in_total"] is True
    assert blob["onsite_days"] == 3


def test_idempotent_second_run_scores_nothing(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")])
    gen = stub_generate(valid_json())
    score_stage.run_score(prefs_path=prefs_path(tmp_path), db_path=db_path, _generate=gen)
    second = score_stage.run_score(prefs_path=prefs_path(tmp_path), db_path=db_path, _generate=gen)
    assert second.considered == 0
    assert second.scored == 0


def test_rescore_redoes_all(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")])
    p = prefs_path(tmp_path)
    score_stage.run_score(prefs_path=p, db_path=db_path, _generate=stub_generate(valid_json()))
    again = score_stage.run_score(
        prefs_path=p, db_path=db_path, rescore=True,
        _generate=stub_generate(valid_json(criteria_scores={n: 2 for n in QUAL_NAMES})),
    )
    assert again.considered == 1
    assert again.scored == 1
    assert _row(db_path, "1")["score_total"] < 50  # the lower re-score landed


def test_retry_once_then_needs_review(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")])
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=sequence_generate("garbage", "still not json"),
    )
    assert summary.needs_review == 1
    assert summary.scored == 0
    row = _row(db_path, "1")
    assert row["score_status"] == "needs_review"
    assert row["score_total"] is None


def test_retry_succeeds_on_second_attempt(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")])
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=sequence_generate("garbage", valid_json()),
    )
    assert summary.scored == 1
    assert _row(db_path, "1")["score_status"] == "scored"


def test_model_error_is_fail_soft(tmp_path):
    db_path = tmp_path / "jobs.db"
    job1 = JobRecord(
        source="wttj", external_id="1", url="http://x/1", title="Broken PM Role",
        company="Acme", location="Paris 11e", contract_type="CDI",
        salary_text="55k", description="d", posted_at=None, lang="en",
    )
    _seed(db_path, [job1, _job("2")])

    def _gen(model, prompt, *, system=None):
        if "Broken PM Role" in prompt:
            raise RuntimeError("model down")
        return FakeGen(valid_json())

    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path, _generate=_gen,
    )
    # One failed (needs_review), the other scored — batch survived.
    assert summary.scored == 1
    assert summary.needs_review == 1
    assert _row(db_path, "1")["score_status"] == "needs_review"
    assert _row(db_path, "2")["score_status"] == "scored"


def test_unknown_commute_flagged_and_dropped(tmp_path):
    db_path = tmp_path / "jobs.db"
    # Seed with no enrichment → commute_minutes is NULL.
    _seed(db_path, [_job("1")], commute_minutes=None)
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=stub_generate(valid_json(criteria_scores={n: 10 for n in QUAL_NAMES})),
    )
    assert summary.commute_unknown == 1
    row = _row(db_path, "1")
    blob = json.loads(row["score_json"])
    assert blob["commute_included_in_total"] is False
    assert any("commute unknown" in f for f in blob["red_flags"])
    # All-10 qualitative with commute dropped → 100 (not dragged by a zero).
    assert row["score_total"] == 100.0


def test_enrichment_independent_unenriched_row_scored(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")], commute_minutes=None)  # scored_at NULL, enriched_at NULL
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=stub_generate(valid_json()),
    )
    assert summary.scored == 1  # scoring did not wait on enrichment


def test_salary_not_stated_red_flag(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1", salary_text=None)])
    score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=stub_generate(valid_json()),
    )
    blob = json.loads(_row(db_path, "1")["score_json"])
    assert any("salary not stated" in f for f in blob["red_flags"])


def test_rejected_offers_not_scored(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")], status="rejected")
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=stub_generate(valid_json()),
    )
    assert summary.considered == 0


def test_needs_review_offers_are_scored(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")], status="needs_review")
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path,
        _generate=stub_generate(valid_json()),
    )
    assert summary.scored == 1  # the scorer owns the needs_review tail


def test_dry_run_writes_nothing(tmp_path):
    db_path = tmp_path / "jobs.db"
    _seed(db_path, [_job("1")])
    summary = score_stage.run_score(
        prefs_path=prefs_path(tmp_path), db_path=db_path, dry_run=True,
        _generate=stub_generate(valid_json()),
    )
    assert summary.scored == 1  # computed
    assert _row(db_path, "1")["score_status"] is None  # but not written


def test_assumed_cdi_note_reaches_prompt(tmp_path):
    db_path = tmp_path / "jobs.db"
    reasons = json.dumps([{"filter": "contract_type", "outcome": "passed",
                           "reason": "contract type not stated; assumed CDI"}])
    _seed(db_path, [_job("1")], filter_reasons=reasons)
    seen = {}

    def _gen(model, prompt, *, system=None):
        seen["prompt"] = prompt
        return FakeGen(valid_json())

    score_stage.run_score(prefs_path=prefs_path(tmp_path), db_path=db_path, _generate=_gen)
    assert "assumed cdi" in seen["prompt"].lower()

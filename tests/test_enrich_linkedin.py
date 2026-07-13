"""Tests for the LinkedIn guest-endpoint enrichment stage.

Fully offline: an httpx.MockTransport serves a saved fixture (or a 429) so no
network is touched. Locks in the behaviors that matter: descriptions get
backfilled, "Full-time" does NOT become a contract, the run is idempotent and
cached (never re-fetches an enriched row), a rate-limit response is fail-soft
(row left needs_review), and enriched rows are auto re-filtered.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from jobscout.models import JobRecord
from jobscout.pipeline import enrich_linkedin
from jobscout.storage import db

FIXTURE = (Path(__file__).parent / "fixtures" / "linkedin_guest_jobposting.html").read_text(
    encoding="utf-8"
)

PREFS_YAML = """\
hard_filters:
  contract_types: ["CDI"]
  salary_floor_eur: 50000
  seniority:
    include_keywords: ["product manager", "product owner", "PM", "PO"]
    exclude_keywords: ["junior", "intern"]
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


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "guest_cache"


def _linkedin_job(external_id, title="AI Product Owner", location="Paris (Hybrid)"):
    return JobRecord(
        source="linkedin_email", external_id=external_id,
        url=f"https://www.linkedin.com/jobs/view/{external_id}/",
        title=title, company="Acme", location=location, contract_type=None,
        salary_text=None, description=None, posted_at=None, lang="en",
    )


def _seed(db_path, jobs):
    conn = db.connect(db_path)
    db.upsert_jobs(conn, jobs)
    # Mark them filtered as needs_review, mimicking a prior `jobscout filter`.
    for j in jobs:
        row = conn.execute(
            "SELECT id FROM jobs WHERE external_id = ?", (j.external_id,)
        ).fetchone()
        db.record_filter_verdict(conn, row["id"], filter_status="needs_review",
                                 filter_reasons_json="[]")
    conn.commit()
    conn.close()


def _client_serving(html, status=200):
    """An httpx.Client whose every request returns the given html/status."""
    def handler(request):
        return httpx.Response(status, text=html)
    return httpx.Client(transport=httpx.MockTransport(handler))


def _run(db_path, prefs_path, cache_dir, client, **kw):
    return enrich_linkedin.run_enrich_linkedin(
        prefs_path=prefs_path, db_path=db_path, cache_dir=cache_dir,
        min_delay=0, max_delay=0, _client=client, **kw
    )


def test_backfills_description(db_path, prefs_path, cache_dir):
    _seed(db_path, [_linkedin_job("111")])
    summary = _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE))
    assert summary.enriched == 1

    conn = db.connect(db_path)
    row = conn.execute("SELECT description, contract_type FROM jobs WHERE external_id='111'").fetchone()
    assert row["description"] and len(row["description"]) > 200
    # "Full-time" is a schedule, not a contract — must stay None.
    assert row["contract_type"] is None
    conn.close()


def test_enriched_row_is_refiltered(db_path, prefs_path, cache_dir):
    _seed(db_path, [_linkedin_job("111")])
    summary = _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE))
    assert summary.refiltered == 1
    conn = db.connect(db_path)
    # Still needs_review (contract unknown), but filtered_at was refreshed and
    # the verdict recomputed against the new description.
    row = conn.execute("SELECT filter_status FROM jobs WHERE external_id='111'").fetchone()
    assert row["filter_status"] in {"needs_review", "passed", "rejected"}
    conn.close()


def test_idempotent_second_run_considers_nothing(db_path, prefs_path, cache_dir):
    _seed(db_path, [_linkedin_job("111")])
    _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE))
    # Second run: the row now has a description, so it's no longer a candidate.
    second = _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE))
    assert second.considered == 0
    assert second.enriched == 0


def test_uses_cache_on_repeat(db_path, prefs_path, cache_dir):
    # Two distinct jobs; enrich the first, then force a scenario where the cache
    # is used: re-seed a fresh DB but keep the cache dir. The second DB run must
    # read the cached HTML rather than the (now 429) client.
    _seed(db_path, [_linkedin_job("111")])
    _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE))
    assert (cache_dir / "111.html").exists()  # cached the raw response

    # New DB, same cache, a client that would 429 if hit — cache must win.
    db_path2 = db_path.parent / "jobs2.db"
    _seed(db_path2, [_linkedin_job("111")])
    summary = _run(db_path2, prefs_path, cache_dir, _client_serving("", status=429))
    assert summary.enriched == 1 and summary.from_cache == 1


def test_rate_limit_is_failsoft(db_path, prefs_path, cache_dir):
    _seed(db_path, [_linkedin_job("111")])
    summary = _run(db_path, prefs_path, cache_dir, _client_serving("", status=429))
    assert summary.enriched == 0 and summary.failed == 1

    conn = db.connect(db_path)
    row = conn.execute("SELECT description, filter_status FROM jobs WHERE external_id='111'").fetchone()
    assert row["description"] is None            # untouched
    assert row["filter_status"] == "needs_review"  # verdict unchanged
    conn.close()
    # A failed fetch is not cached, so a later run can retry.
    assert not (cache_dir / "111.html").exists()


def test_dry_run_writes_nothing(db_path, prefs_path, cache_dir):
    _seed(db_path, [_linkedin_job("111")])
    summary = _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE), dry_run=True)
    assert summary.enriched == 1  # parsed successfully
    conn = db.connect(db_path)
    row = conn.execute("SELECT description FROM jobs WHERE external_id='111'").fetchone()
    assert row["description"] is None  # but nothing written
    conn.close()


def test_limit_caps_fetches(db_path, prefs_path, cache_dir):
    _seed(db_path, [_linkedin_job("111"), _linkedin_job("222"), _linkedin_job("333")])
    summary = _run(db_path, prefs_path, cache_dir, _client_serving(FIXTURE), limit=2)
    assert summary.considered == 2


def test_resolve_contract_full_time_is_none():
    # The deliberate non-inference: a schedule is not a contract.
    assert enrich_linkedin._resolve_contract("Full-time") is None
    assert enrich_linkedin._resolve_contract("Part-time") is None
    assert enrich_linkedin._resolve_contract(None) is None
    # A value that genuinely carries contract meaning still maps through.
    assert enrich_linkedin._resolve_contract("Temporary") == "CDD"

"""Route smoke tests for the webapp (web/app.py + routes.py).

Offline: a temp DB seeded with one scored + one unscored offer, and
Settings pointed at the committed preferences.example.yaml so criteria rendering
works without the gitignored real file. Asserts the read-only contract (GET-only
routes, the fragment is a bare table, 404 for a missing id) and that the detail
page renders the score + company blocks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.models import JobRecord
from jobscout.storage import db
from jobscout.web.app import create_app
from jobscout.web.settings import Settings

_PREFS = Path(__file__).resolve().parents[1] / "preferences.example.yaml"


def _job(ext, **kw):
    d = dict(
        source="wttj", external_id=ext, url=f"https://x/{ext}", title="Product Manager",
        company="ACME", location="Paris", contract_type="CDI", salary_text=None,
        description="A PM role.", posted_at=None, lang="fr",
    )
    d.update(kw)
    return JobRecord(**d)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = db.connect(db_path)
    db.upsert_jobs(conn, [_job("a"), _job("b", company="BetaCorp")])
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM jobs")}
    db.record_filter_verdict(conn, ids["a"], filter_status="passed", filter_reasons_json="[]")
    db.record_filter_verdict(conn, ids["b"], filter_status="passed", filter_reasons_json="[]")
    db.record_score(
        conn, ids["a"], score_total=82.5, score_status="scored",
        score_json=json.dumps({
            "reasoning": "Strong fit for a B2B SaaS PM.",
            "red_flags": ["salary not stated"],
            "criteria_scores": {"product_culture_and_management": 8},
            "weekly_commute_fit": 6.0, "onsite_days": 3, "remote_policy": "hybrid",
            "commute_included_in_total": True,
        }),
    )
    db.record_company_brief(conn, ids["a"], company_brief_json=json.dumps(
        {"summary": "ACME builds AI workflow tools.", "product": "Automation platform"}))
    conn.commit()
    conn.close()

    settings = Settings(
        project_root=tmp_path, db_path=db_path, preferences_path=_PREFS,
        env_path=tmp_path / ".env",
    )
    # Context-manager form runs the lifespan, so app.state.runner exists (the
    # dashboard/detail routes read runner.snapshot()). The worker thread idles
    # until something is enqueued, so this is cheap for these read-only tests.
    with TestClient(create_app(settings)) as tc:
        yield tc, ids


def test_healthz(client):
    tc, _ = client
    r = tc.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_dashboard_shows_total(client):
    tc, _ = client
    r = tc.get("/")
    assert r.status_code == 200
    assert "Total offers" in r.text and ">2<" in r.text.replace(" ", "").replace("\n", "")


def test_offers_list_ok(client):
    tc, _ = client
    r = tc.get("/offers")
    assert r.status_code == 200
    assert "Product Manager" in r.text and "BetaCorp" in r.text


def test_offers_table_fragment_is_bare(client):
    tc, _ = client
    r = tc.get("/offers/table?sort=score_total&dir=desc")
    assert r.status_code == 200
    assert "<table" in r.text
    assert "<html" not in r.text.lower()  # a fragment, not a full page


def test_offers_table_sort_by_criterion(client):
    tc, _ = client
    # role_scope_and_focus exists in the committed preferences.example.yaml stub.
    r = tc.get("/offers/table?sort=criteria:role_scope_and_focus")
    assert r.status_code == 200


def test_offers_table_bad_sort_is_400(client):
    tc, _ = client
    r = tc.get("/offers/table?sort=nonsense")
    assert r.status_code == 400


def test_offer_detail_renders_blocks(client):
    tc, ids = client
    r = tc.get(f"/offers/{ids['a']}")
    assert r.status_code == 200
    assert "Strong fit for a B2B SaaS PM." in r.text  # verdict
    assert "ACME builds AI workflow tools." in r.text  # company summary
    assert "salary not stated" in r.text               # red flag
    assert "View original posting" in r.text           # outbound link


def test_offer_detail_404(client):
    tc, _ = client
    r = tc.get("/offers/999999")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


# --- V2 write routes ------------------------------------------------------


def test_set_review_writes_and_returns_fragment(client):
    tc, ids = client
    r = tc.post(f"/offers/{ids['a']}/review", data={"disposition": "applied", "notes": "call"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()  # control fragment, not a full page
    assert "applied" in r.text
    # dashboard now counts it
    assert ">1<" in tc.get("/").text.replace(" ", "")


def test_set_review_bad_disposition_400(client):
    tc, ids = client
    r = tc.post(f"/offers/{ids['a']}/review", data={"disposition": "bogus"})
    assert r.status_code == 400


def test_set_review_missing_offer_404(client):
    tc, _ = client
    r = tc.post("/offers/999999/review", data={"disposition": "applied"})
    assert r.status_code == 404


def test_offers_table_disposition_filter(client):
    tc, ids = client
    tc.post(f"/offers/{ids['a']}/review", data={"disposition": "applied"})
    applied = tc.get("/offers/table?disposition=applied").text
    assert "Product Manager" in applied  # offer 'a' is a PM titled row
    unreviewed = tc.get("/offers/table?disposition=__unreviewed__").text
    # offer 'a' is now reviewed, so BetaCorp (b) is the unreviewed one
    assert "BetaCorp" in unreviewed


def test_run_status_and_trigger(client):
    tc, _ = client
    assert tc.get("/runs/status").status_code == 200
    # a fast, safe stage (commute-only recompute from stored score; no LLM/net)
    r = tc.post("/runs/score-commute-only")
    assert r.status_code == 200


def test_trigger_unknown_stage_404(client):
    tc, _ = client
    assert tc.post("/runs/nonsense").status_code == 404


def test_rescore_route(client):
    tc, ids = client
    assert tc.post(f"/offers/{ids['a']}/rescore").status_code == 200
    assert tc.post("/offers/999999/rescore").status_code == 404


def test_require_local_origin(client):
    tc, ids = client
    # A foreign Origin is refused; no Origin is allowed (default in TestClient).
    bad = tc.post(
        f"/offers/{ids['a']}/review",
        data={"disposition": "applied"},
        headers={"origin": "http://evil.example"},
    )
    assert bad.status_code == 403

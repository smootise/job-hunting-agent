"""Tests for the two Phase 3 stage orchestrators + the brief-in-scoring hook.

Fully offline: scripted model, httpx.MockTransport clients, temp DB seeded the
way `jobscout filter` leaves it. Covers company research (draft→ground→persist,
idempotency, dry-run, WTTJ count, fail-soft), address research (needs_address→
agent→validate→persist with commute left NULL, only-the-tail scope), and that a
persisted brief reaches the scoring prompt as a fenced advisory block.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from jobscout.models import JobRecord
from jobscout.pipeline import research_address, research_company, scoring
from jobscout.storage import db

ENV = {"SEARXNG_URL": "http://nas:8085", "GOOGLE_ROUTES_KEY": "k"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("jobscout.config.load_env", lambda *a, **k: dict(ENV))


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "jobs.db"


def _seed(db_path, job, *, status="passed"):
    conn = db.connect(db_path)
    db.upsert_jobs(conn, [job])
    row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (job.external_id,)).fetchone()
    db.record_filter_verdict(conn, row["id"], filter_status=status, filter_reasons_json="[]")
    conn.commit()
    conn.close()


def _wttj_job(**kw):
    d = dict(source="wttj", external_id="e1",
             url="https://www.welcometothejungle.com/fr/companies/acme/jobs/pm",
             title="Product Manager", company="ACME", location="Paris",
             contract_type="CDI", salary_text=None, description="A PM role.",
             posted_at=None, lang="fr")
    d.update(kw)
    return JobRecord(**d)


def _gen(scripted):
    calls = {"i": 0}

    def generate(model, prompt, *, system=None, log_dir=None):
        text = scripted[min(calls["i"], len(scripted) - 1)]
        calls["i"] += 1
        return SimpleNamespace(response=text, log_path=log_dir)

    return generate


def _search_client():
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"results": [{"title": "ACME", "url": "https://acme.fr", "content": "AI"}]})))


def _fetch_client():
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html><body>ACME builds AI workflow automation, ~200 employees.</body></html>",
                                 headers={"content-type": "text/html"})))


# --- company research ----------------------------------------------------

_DRAFT = ('{"final": {"summary": "AI workflow automation", "product": "wf tool", "culture": null, '
          '"size_signal": "~200 employees", "ai_usage": "LLM", "sources": ["https://acme.fr"], "confidence": "high"}}')
_GROUNDED = ('{"summary": "AI workflow automation", "product": "wf tool", "culture": null, '
             '"size_signal": "~200 employees", "ai_usage": "LLM", "sources": ["https://acme.fr"], "confidence": "medium"}')


def test_research_company_persists_brief(db_path):
    _seed(db_path, _wttj_job())
    s = research_company.run_research_company(
        db_path=db_path, _generate=_gen([_DRAFT, _GROUNDED]),
        _search_client=_search_client(), _fetch_client=_fetch_client())
    assert s.considered == 1 and s.briefed == 1 and s.from_wttj == 1
    conn = db.connect(db_path)
    row = conn.execute("SELECT company_brief, company_researched_at FROM jobs").fetchone()
    conn.close()
    assert row["company_researched_at"] is not None
    assert "AI workflow automation" in row["company_brief"]


def _fetch_client_wttj_403():
    """Fetch client that 403s the WTTJ company page (the real Doctolib case)."""
    def handler(request):
        if "welcometothejungle.com" in str(request.url):
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text="<html><body>ACME builds AI workflow automation.</body></html>",
                              headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_brief_survives_when_wttj_403s(db_path):
    # Regression for the Doctolib smoke run: WTTJ profile 403s, agent searches but
    # the description + snippets are still evidence, so grounding keeps the brief
    # instead of nulling everything to needs_review.
    _seed(db_path, _wttj_job(description="A Product Manager role at an AI workflow automation company, ~200 employees."))
    # Grounding keeps the summary (supported by the description/snippets).
    grounded = ('{"summary": "AI workflow automation", "product": null, "culture": null, '
                '"size_signal": "~200 employees", "ai_usage": null, "sources": [], "confidence": "low"}')
    s = research_company.run_research_company(
        db_path=db_path, _generate=_gen([_DRAFT, grounded]),
        _search_client=_search_client(), _fetch_client=_fetch_client_wttj_403())
    assert s.briefed == 1 and s.needs_review == 0 and s.from_wttj == 0  # no WTTJ profile, still briefed
    conn = db.connect(db_path)
    row = conn.execute("SELECT company_brief FROM jobs").fetchone()
    conn.close()
    assert "AI workflow automation" in row["company_brief"]


def test_fetched_page_reaches_grounding_even_if_not_cited(db_path):
    # Regression for the deeper Doctolib bug: grounding must see the page the agent
    # actually FETCHED, not re-match the draft's cited URLs (the model cited URLs it
    # never fetched, leaving grounding with a bare page title). Here the agent
    # fetches acme.fr (body: PAGE_MARKER) but the draft cites a *different* URL; the
    # fetched body must still appear in the grounding prompt.
    _seed(db_path, _wttj_job(description="short"))
    PAGE_MARKER = "UNIQUEBODYTOKEN ACME builds AI workflow automation for enterprises"

    def fetch_handler(request):
        if "welcometothejungle.com" in str(request.url):
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text=f"<html><body>{PAGE_MARKER}</body></html>",
                              headers={"content-type": "text/html"})

    # Draft: agent fetches acme.fr, then cites a URL it never fetched.
    draft = ('{"tool": "fetch_page", "args": {"url": "https://acme.fr/about"}}',
             '{"final": {"summary": "AI workflow automation", "product": null, "culture": null, '
             '"size_signal": null, "ai_usage": null, "sources": ["https://never-fetched.example"], "confidence": "medium"}}')
    grounded = ('{"summary": "AI workflow automation", "product": null, "culture": null, '
                '"size_signal": null, "ai_usage": null, "sources": [], "confidence": "low"}')

    captured = {}

    def spy_gen(scripted):
        base = _gen(scripted)

        def generate(model, prompt, *, system=None, log_dir=None):
            r = base(model, prompt, system=system, log_dir=log_dir)
            if "DRAFT BRIEF" in prompt:      # the grounding call
                captured["grounding_prompt"] = prompt
            return r

        return generate

    research_company.run_research_company(
        db_path=db_path, _generate=spy_gen([*draft, grounded]),
        _search_client=_search_client(), _fetch_client=httpx.Client(transport=httpx.MockTransport(fetch_handler)))

    assert "grounding_prompt" in captured
    # The FETCHED page body must be in the grounding evidence, despite not being cited.
    assert PAGE_MARKER in captured["grounding_prompt"]


def test_research_company_idempotent(db_path):
    _seed(db_path, _wttj_job())
    kw = dict(db_path=db_path, _search_client=_search_client(), _fetch_client=_fetch_client())
    research_company.run_research_company(_generate=_gen([_DRAFT, _GROUNDED]), **kw)
    s2 = research_company.run_research_company(_generate=_gen([_DRAFT, _GROUNDED]), **kw)
    assert s2.considered == 0


def test_research_company_dry_run_writes_nothing(db_path):
    _seed(db_path, _wttj_job())
    research_company.run_research_company(
        db_path=db_path, dry_run=True, _generate=_gen([_DRAFT, _GROUNDED]),
        _search_client=_search_client(), _fetch_client=_fetch_client())
    conn = db.connect(db_path)
    row = conn.execute("SELECT company_researched_at FROM jobs").fetchone()
    conn.close()
    assert row["company_researched_at"] is None


def test_research_company_only_passed_scope(db_path):
    _seed(db_path, _wttj_job(), status="rejected")
    s = research_company.run_research_company(
        db_path=db_path, _generate=_gen([_DRAFT, _GROUNDED]),
        _search_client=_search_client(), _fetch_client=_fetch_client())
    assert s.considered == 0  # rejected offers are never researched


# --- address research ----------------------------------------------------


def _ban_client():
    def handler(request):
        q = request.url.params.get("q", "")
        if "Rivoli" in q:
            return httpx.Response(200, json={"features": [{
                "geometry": {"coordinates": [2.35, 48.85]},
                "properties": {"label": "12 Rue de Rivoli 75001 Paris", "city": "Paris",
                               "postcode": "75001", "score": 0.9, "context": "75, Paris"}}]})
        return httpx.Response(200, json={"features": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


_ADDR_FINAL = ('{"final": {"address": "12 rue de Rivoli, 75001 Paris", "confidence": "medium", '
               '"evidence_url": "https://acme.fr", "reasoning": "found"}}')


def test_research_address_resolves_tail(db_path):
    # bare "Paris" -> resolve_address returns needs_address -> agent runs
    _seed(db_path, _wttj_job(location="Paris"))
    s = research_address.run_research_address(
        db_path=db_path, _generate=_gen([_ADDR_FINAL]),
        _search_client=_search_client(), _fetch_client=_fetch_client(), _geo_client=_ban_client())
    assert s.needed_agent == 1 and s.resolved == 1
    conn = db.connect(db_path)
    row = conn.execute("SELECT address_source, lat, commute_minutes FROM jobs").fetchone()
    conn.close()
    assert row["address_source"] == "agent"
    assert row["lat"] == 48.85
    assert row["commute_minutes"] is None  # routing is enrich-commute's job


def test_research_address_skips_placeable_rows(db_path):
    # A specific address in the description -> resolve_address places it -> agent skipped.
    _seed(db_path, _wttj_job(location="Boulogne-Billancourt",
                             description="Bureaux au 5 rue de Rivoli, 75001 Paris."))
    s = research_address.run_research_address(
        db_path=db_path, _generate=_gen([_ADDR_FINAL]),
        _search_client=_search_client(), _fetch_client=_fetch_client(), _geo_client=_ban_client())
    assert s.needed_agent == 0  # deterministic chain placed it; agent not needed


# --- brief reaches the scorer -------------------------------------------


def test_persisted_brief_flows_into_scoring_prompt(db_path):
    _seed(db_path, _wttj_job())
    research_company.run_research_company(
        db_path=db_path, _generate=_gen([_DRAFT, _GROUNDED]),
        _search_client=_search_client(), _fetch_client=_fetch_client())

    conn = db.connect(db_path)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    conn.close()

    from jobscout.pipeline.score_stage import _company_brief
    brief = _company_brief(row["company_brief"])
    rec = JobRecord.from_row(row)
    crit = [scoring.Criterion("ai_ml_focus", 8, "ai")]
    _system, user = scoring.build_prompt(rec, crit, "ideal", company_brief=brief)
    assert "<<<COMPANY_RESEARCH" in user
    assert "AI workflow automation" in user

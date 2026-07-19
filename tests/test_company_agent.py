"""Tests for the company-research agent + the grounding-verification pass.

Fully offline: scripted model, plain fetch stubs. Covers the deterministic WTTJ
profile URL/fetch, the draft loop, and — the quality gate — the grounding pass:
claims the sources don't support are stripped, a brief grounded to nothing is
flagged needs_review, and any error is fail-soft to needs_review (never a raise).
"""

from __future__ import annotations

from types import SimpleNamespace

from jobscout.agents import company_agent as ca
from jobscout.models import JobRecord


def _rec(**kw):
    d = dict(source="wttj", external_id="1",
             url="https://www.welcometothejungle.com/fr/companies/acme/jobs/pm",
             title="PM", company="ACME", location="Paris", contract_type="CDI",
             salary_text=None, description="desc", posted_at=None, lang="fr")
    d.update(kw)
    return JobRecord(**d)


def _gen(scripted):
    calls = {"i": 0}

    def generate(model, prompt, *, system=None, log_dir=None):
        text = scripted[min(calls["i"], len(scripted) - 1)]
        calls["i"] += 1
        return SimpleNamespace(response=text, log_path=log_dir)

    return generate


# --- step 1: deterministic WTTJ profile ---------------------------------


def test_wttj_profile_url_from_wttj_offer():
    assert ca.wttj_profile_url(_rec()) == "https://www.welcometothejungle.com/fr/companies/acme"


def test_wttj_profile_url_none_for_non_wttj():
    assert ca.wttj_profile_url(_rec(source="france_travail")) is None


def test_wttj_profile_url_none_for_unparseable():
    assert ca.wttj_profile_url(_rec(url="https://x.com/no-slug")) is None


def test_fetch_wttj_profile_returns_text():
    url, text = ca.fetch_wttj_profile(_rec(), fetch=lambda u, **k: "ACME builds AI. ~200 employees.")
    assert url.endswith("/companies/acme") and text.startswith("ACME builds AI")


def test_fetch_wttj_profile_failsoft_on_refusal():
    url, text = ca.fetch_wttj_profile(_rec(), fetch=lambda u, **k: "(fetch refused: x)")
    assert url is not None and text is None


# --- steps 2-3: the draft loop ------------------------------------------

_DRAFT = ('{"final": {"summary": "AI workflow automation", "product": "workflow tool", '
          '"culture": "collaborative", "size_signal": "~200 employees", '
          '"ai_usage": "LLM automation", "sources": ["https://acme.fr"], "confidence": "high"}}')


def _tools():
    return {
        "web_search": lambda query: [{"title": "ACME", "url": "https://acme.fr", "snippet": "AI"}],
        "fetch_page": lambda url: "ACME builds AI workflow tools, ~200 employees",
    }


def test_research_company_returns_draft():
    draft = ca.research_company(_rec(), tools_map=_tools(), wttj_text="ACME builds AI", generate=_gen([_DRAFT]))
    assert draft["summary"] == "AI workflow automation"


def test_research_company_none_when_no_final():
    draft = ca.research_company(_rec(), tools_map=_tools(), generate=_gen(["garbage"]), max_steps=1)
    assert draft is None


# --- step 4: grounding-verification pass --------------------------------


def test_verify_strips_unsupported_claim():
    draft = {"summary": "AI workflow automation", "product": "wf tool", "culture": "great perks",
             "size_signal": "~200 employees", "ai_usage": "LLM", "sources": ["https://acme.fr"], "confidence": "high"}
    # Grounding model drops 'culture' (sources didn't support it).
    grounded = ('{"summary": "AI workflow automation", "product": "wf tool", "culture": null, '
                '"size_signal": "~200 employees", "ai_usage": "LLM", "sources": ["https://acme.fr"], "confidence": "medium"}')
    brief = ca.verify_brief(draft, ["ACME builds AI workflow tools, ~200 employees"], generate=_gen([grounded]))
    assert brief.culture is None
    assert brief.summary == "AI workflow automation"
    assert brief.confidence == "medium"
    assert not brief.needs_review


def test_verify_empty_after_grounding_needs_review():
    draft = {"summary": "x", "product": "y", "sources": []}
    grounded = ('{"summary": null, "product": null, "culture": null, "size_signal": null, '
                '"ai_usage": null, "sources": [], "confidence": "low"}')
    brief = ca.verify_brief(draft, ["unrelated"], generate=_gen([grounded]))
    assert brief.needs_review and brief.is_empty


def test_verify_no_evidence_needs_review_without_model_call():
    # The Doctolib-smoke failure mode: a good draft but ZERO source texts. That's
    # the honest "can't verify anything" case → needs_review, and we must not even
    # call the model (nothing to ground against).
    def must_not_call(model, prompt, *, system=None, log_dir=None):
        raise AssertionError("must not call the model with no evidence")

    brief = ca.verify_brief({"summary": "x"}, [], generate=must_not_call)
    assert brief.needs_review and brief.is_empty


def test_verify_survives_on_description_only_no_wttj():
    # The fix: a missing WTTJ profile must NOT sink the brief when we still have
    # the job description as evidence. Here the only source is the description and
    # the grounding model keeps the supported claim.
    draft = {"summary": "AI workflow automation", "product": None, "culture": None,
             "size_signal": None, "ai_usage": None, "sources": []}
    grounded = ('{"summary": "AI workflow automation", "product": null, "culture": null, '
                '"size_signal": null, "ai_usage": null, "sources": [], "confidence": "low"}')
    brief = ca.verify_brief(draft, ["The role is at an AI workflow automation company."], generate=_gen([grounded]))
    assert not brief.needs_review
    assert brief.summary == "AI workflow automation"


def test_verify_blank_only_sources_treated_as_no_evidence():
    # Whitespace-only "sources" are not evidence → needs_review, no model call.
    def must_not_call(model, prompt, *, system=None, log_dir=None):
        raise AssertionError("blank sources are not evidence")

    assert ca.verify_brief({"summary": "x"}, ["", "   "], generate=must_not_call).needs_review


def test_verify_none_draft_needs_review():
    assert ca.verify_brief(None, [], generate=_gen(["x"])).needs_review


def test_verify_bad_json_needs_review():
    assert ca.verify_brief({"summary": "x"}, [], generate=_gen(["not json at all"])).needs_review


def test_verify_generate_error_is_failsoft():
    def boom(model, prompt, *, system=None, log_dir=None):
        raise RuntimeError("model down")

    brief = ca.verify_brief({"summary": "x"}, [], generate=boom)
    assert brief.needs_review  # error → needs_review, not a crash


def test_brief_as_json_roundtrip():
    b = ca.CompanyBrief("s", "p", None, "~200", "ai", ["u"], "high")
    d = b.as_json_dict()
    assert d["summary"] == "s" and d["culture"] is None and d["needs_review"] is False

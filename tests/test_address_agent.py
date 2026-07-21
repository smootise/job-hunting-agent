"""Tests for the address-research agent + its deterministic validation net.

Fully offline: scripted model, plain tool functions, BAN geocode monkeypatched.
Covers candidate parsing (incl. honest 'not found'), and the validation net —
the load-bearing safety property: only an address that geocodes AND lands in
Île-de-France is trusted; a hallucinated/out-of-region/non-geocoding candidate is
rejected (a wrong address can never hard-reject an offer). Also the confidence
cap and the enrich-commute reconstruction of an agent-stored address.
"""

from __future__ import annotations

from types import SimpleNamespace


from jobscout.agents import address_agent as aa
from jobscout.enrich import geocode
from jobscout.pipeline import enrich_commute


def _gen(scripted):
    calls = {"i": 0}

    def generate(model, prompt, *, system=None, log_dir=None):
        text = scripted[min(calls["i"], len(scripted) - 1)]
        calls["i"] += 1
        return SimpleNamespace(response=text, log_path=log_dir)

    return generate


def _tools():
    return {
        "web_search": lambda query: [{"title": "ACME", "url": "https://www.societe.com/acme", "snippet": "12 rue de Rivoli"}],
        "fetch_page": lambda url: "ACME siege 12 rue de Rivoli, 75001 Paris",
    }


def _ban(monkeypatch):
    """Monkeypatch geocode: in-IDF for 'Rivoli', out for 'Lyon', None otherwise."""
    def fake(query, *, postcode=None, limit=1, client=None, timeout_seconds=15.0):
        if "Rivoli" in query:
            return geocode.GeocodeResult("12 Rue de Rivoli 75001 Paris", 48.85, 2.35, "Paris", "75001", 0.9, "75")
        if "Lyon" in query:
            return geocode.GeocodeResult("X 69001 Lyon", 45.75, 4.85, "Lyon", "69001", 0.9, "69")
        return None

    monkeypatch.setattr(aa.geocode, "geocode", fake)


def test_research_address_parses_found_candidate():
    g = _gen([
        '{"tool": "web_search", "args": {"query": "acme"}}',
        '{"final": {"address": "12 rue de Rivoli, 75001 Paris", "confidence": "high", "evidence_url": "https://www.societe.com/acme", "reasoning": "found"}}',
    ])
    cand = aa.research_address("ACME", tools_map=_tools(), generate=g)
    assert cand.address == "12 rue de Rivoli, 75001 Paris"
    assert cand.confidence == "high"


def test_research_address_honest_not_found():
    g = _gen(['{"final": {"address": null, "confidence": "low", "evidence_url": null, "reasoning": "no office found"}}'])
    cand = aa.research_address("Obscure Co", tools_map=_tools(), generate=g)
    assert cand.address is None


def test_research_address_no_final_returns_none():
    g = _gen(["garbage", "garbage", "garbage", "garbage", "garbage", "garbage"])
    assert aa.research_address("X", tools_map=_tools(), generate=g, max_steps=2) is None


def test_validate_accepts_in_idf_and_caps_confidence(monkeypatch):
    _ban(monkeypatch)
    cand = aa.AddressCandidate("12 rue de Rivoli, 75001 Paris", "high", "http://e", "r")
    v = aa.validate_candidate(cand)
    assert v is not None
    assert (v.lat, v.lon) == (48.85, 2.35)
    assert v.confidence == "medium"  # 'high' capped — web-found is never 'high'


def test_validate_rejects_out_of_idf(monkeypatch):
    _ban(monkeypatch)
    cand = aa.AddressCandidate("1 rue de Lyon, 69001 Lyon", "medium", None, "r")
    assert aa.validate_candidate(cand) is None


def test_validate_rejects_non_geocoding(monkeypatch):
    _ban(monkeypatch)
    assert aa.validate_candidate(aa.AddressCandidate("nowhere at all", "low", None, "r")) is None


def test_validate_rejects_null_address():
    assert aa.validate_candidate(aa.AddressCandidate(None, "low", None, "r")) is None
    assert aa.validate_candidate(None) is None


def test_agent_resolution_reconstructs_stored_address():
    row = {"address_source": "agent", "lat": 48.85, "lon": 2.35,
           "address": "12 Rue de Rivoli", "address_confidence": "medium"}
    r = enrich_commute._agent_resolution(row)
    assert r is not None and r.is_resolved and r.source == "agent" and r.in_idf


def test_agent_resolution_ignores_non_agent_rows():
    row = {"address_source": "wttj", "lat": 1.0, "lon": 2.0, "address": "x", "address_confidence": None}
    assert enrich_commute._agent_resolution(row) is None


def test_agent_resolution_defensive_on_missing_coords():
    row = {"address_source": "agent", "lat": None, "lon": None, "address": "x", "address_confidence": "medium"}
    assert enrich_commute._agent_resolution(row) is None

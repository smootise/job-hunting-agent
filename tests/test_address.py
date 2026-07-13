"""Tests for the deterministic address-resolution chain.

Offline: BAN is mocked via the injected client (an httpx.MockTransport keyed on
the query string). Covers the remote short-circuit, precise-street-from-prose,
city fallback, the out-of-region → approximate flag, and unresolved.
"""

from __future__ import annotations

import json

import httpx

from jobscout.enrich import address
from jobscout.models import JobRecord


def _job(location=None, description=None, source="wttj", remote_tag=False):
    loc = location
    if remote_tag and location:
        loc = f"{location} (remote: fulltime)"
    return JobRecord(
        source=source, external_id="1", url="http://x", title="Product Manager",
        company="Acme", location=loc, contract_type="CDI", salary_text=None,
        description=description, posted_at=None, lang="fr",
    )


def _ban(city, postcode, lon, lat, label=None):
    return {
        "features": [{
            "geometry": {"coordinates": [lon, lat]},
            "properties": {
                "label": label or city, "city": city, "postcode": postcode,
                "context": f"{postcode[:2]}, Dept, Region", "score": 0.9,
            },
        }]
    }


def _client_by_query(mapping, default_empty=True):
    """MockTransport that returns a BAN payload chosen by the `q` param.

    `mapping` is a list of (substring, payload) — first substring found in the
    query wins. Unmatched queries return an empty feature set (miss).
    """
    def handler(request):
        q = request.url.params.get("q", "").lower()
        for needle, payload in mapping:
            if needle in q:
                return httpx.Response(200, text=json.dumps(payload))
        empty = {"features": []}
        return httpx.Response(200, text=json.dumps(empty))
    return httpx.Client(transport=httpx.MockTransport(handler))


VIROFLAY = _ban("Viroflay", "78220", 2.1657, 48.8009)
PARIS = _ban("Paris", "75002", 2.35, 48.86)
LYON = _ban("Lyon", "69002", 4.83, 45.76)


def test_fully_remote_short_circuits_without_geocoding():
    job = _job(location="Full remote", description="Poste 100% télétravail, full remote.")

    def boom(request):
        raise AssertionError("remote offers must not geocode")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    result = address.resolve_address(job, client=client)
    assert result.is_remote and not result.is_resolved


def test_precise_street_from_description():
    job = _job(
        location="Paris, Île-de-France",
        description="Nos bureaux: 3 avenue du Général Leclerc, au coeur de la ville.",
    )
    client = _client_by_query([("général leclerc", VIROFLAY), ("paris", PARIS)])
    result = address.resolve_address(job, client=client)
    assert result.source == "posting"
    assert result.confidence == "high"
    assert result.lat == 48.8009 and result.is_resolved


def test_city_fallback_medium_confidence():
    job = _job(location="Paris, Île-de-France")
    client = _client_by_query([("paris", PARIS)])
    result = address.resolve_address(job, client=client)
    assert result.source == "wttj"  # tagged with the offer's source
    assert result.confidence == "medium"
    assert result.city == "Paris" and result.in_idf


def test_out_of_region_city_flagged_approximate():
    job = _job(location="Lyon")
    client = _client_by_query([("lyon", LYON)])
    result = address.resolve_address(job, client=client)
    assert result.source == "approximate"
    assert result.confidence == "low"
    assert result.in_idf is False
    assert result.is_resolved  # still has coords, just flagged — never rejected


def test_street_miss_falls_back_to_city():
    # A street is present in prose but doesn't geocode; the city still does.
    # The street sits in a different city name so it can't accidentally match the
    # "paris" needle in the mock (the street query would be "…, Lille").
    job = _job(
        location="Lille",
        description="Bureaux au 999 rue Introuvable.",
    )
    LILLE = _ban("Lille", "59000", 3.06, 50.63)
    # The city query ("lille") resolves; the street query ("rue introuvable, lille")
    # also contains "lille" — so make the street resolution miss by keying the
    # payload only on the bare city token via a stricter needle.
    def handler(request):
        q = request.url.params.get("q", "").lower()
        if q == "lille":  # exact city query only
            return httpx.Response(200, text=json.dumps(LILLE))
        return httpx.Response(200, text=json.dumps({"features": []}))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = address.resolve_address(job, client=client)
    # Out-of-IDF city, but resolved via the city step, flagged approximate.
    assert result.source == "approximate" and result.confidence == "low"


def test_no_location_is_unresolved():
    job = _job(location=None, description=None)
    client = _client_by_query([])
    result = address.resolve_address(job, client=client)
    assert result.source == "unresolved" and not result.is_resolved


def test_remote_tag_stripped_from_location():
    # "(remote: …)" tail shouldn't leak into the geocode query, but a hybrid tag
    # doesn't make the offer fully-remote, so we still resolve the city.
    job = _job(location="Paris, Île-de-France", remote_tag=True,
               description="2 jours de télétravail par semaine.")
    captured = {}

    def handler(request):
        captured["q"] = request.url.params.get("q", "")
        return httpx.Response(200, text=json.dumps(PARIS))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = address.resolve_address(job, client=client)
    assert "remote:" not in captured["q"].lower()
    assert result.is_resolved

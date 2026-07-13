"""Tests for the BAN geocoder — fully offline via an httpx.MockTransport.

Locks in: the [lon, lat] coordinate-order unpacking (the classic GeoJSON foot-
gun), the IDF region predicate (both in-region and out-of-region), department
extraction from postcode vs. context, and the fail-soft returns (empty query,
no features, transport error → None, never a raise).
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobscout.enrich import geocode


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ban_feature(*, lon, lat, label, city, postcode, context, score=0.95):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "label": label,
                    "city": city,
                    "postcode": postcode,
                    "context": context,
                    "score": score,
                    "type": "housenumber",
                },
            }
        ],
    }


def _serving(payload, status=200):
    def handler(request):
        return httpx.Response(status, text=json.dumps(payload))

    return _client(handler)


def test_geocode_parses_coords_and_region():
    payload = _ban_feature(
        lon=2.1657, lat=48.8009,
        label="3 Avenue du Général Leclerc 78220 Viroflay",
        city="Viroflay", postcode="78220", context="78, Yvelines, Île-de-France",
    )
    result = geocode.geocode("3 av leclerc viroflay", client=_serving(payload))
    assert result is not None
    # [lon, lat] must be unpacked in the right order.
    assert result.lat == pytest.approx(48.8009)
    assert result.lon == pytest.approx(2.1657)
    assert result.city == "Viroflay"
    assert result.department == "78"
    assert result.in_idf is True


def test_geocode_out_of_region_flagged_not_dropped():
    # Lyon: a valid geocode, but NOT Île-de-France — the caller must be able to
    # reject it via .in_idf; geocode itself still returns the result.
    payload = _ban_feature(
        lon=4.8357, lat=45.7640, label="Place Bellecour 69002 Lyon",
        city="Lyon", postcode="69002", context="69, Rhône, Auvergne-Rhône-Alpes",
    )
    result = geocode.geocode("place bellecour lyon", client=_serving(payload))
    assert result is not None
    assert result.department == "69"
    assert result.in_idf is False


def test_department_from_context_when_postcode_missing():
    payload = _ban_feature(
        lon=2.35, lat=48.85, label="Paris", city="Paris",
        postcode=None, context="75, Paris, Île-de-France",
    )
    result = geocode.geocode("paris", client=_serving(payload))
    assert result.department == "75" and result.in_idf


def test_empty_query_returns_none_without_calling():
    # Should short-circuit before any HTTP call — a client that would explode
    # proves we never hit it.
    def boom(request):
        raise AssertionError("must not be called for an empty query")

    assert geocode.geocode("   ", client=_client(boom)) is None


def test_no_features_returns_none():
    empty = {"type": "FeatureCollection", "features": []}
    assert geocode.geocode("nowhere zzz", client=_serving(empty)) is None


def test_transport_error_is_failsoft():
    def handler(request):
        raise httpx.ConnectError("network down")

    assert geocode.geocode("paris", client=_client(handler)) is None


def test_http_error_is_failsoft():
    assert geocode.geocode("paris", client=_serving({}, status=500)) is None

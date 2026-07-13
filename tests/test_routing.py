"""Tests for the Google Routes wrapper + three-strategy commute planner.

Fully offline: an httpx.MockTransport inspects each request's travelMode (and
transitPreferences) and returns a canned Routes response, while RECORDING which
modes were requested — so we can assert the null/0-disables rule actually
prevents bike API calls, not just discards their result.

Coverage:
  - duration string "…s" → minutes; [lon,lat]-agnostic (we pass coords through);
  - three strategies computed and the fastest chosen;
  - max_bike_distance_km 0/null → NO bike/rail-hybrid request is issued;
  - min_bike_walk_minutes gate: short walk connector stays a walk, long one is
    biked; a connector over max distance stays a (long) walk;
  - fail-soft: a 500 for one mode drops just that strategy;
  - SECURITY: home coordinates never appear in any log output.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from jobscout.config import CommutePrefs
from jobscout.enrich import routing
from jobscout.enrich.routing import Coordinates

HOME = Coordinates(lat=48.8009, lon=2.1657)   # Viroflay-ish
OFFICE = Coordinates(lat=48.8698, lon=2.3078)  # Paris-ish
KEY = "test-key"


class RecordingTransport:
    """Serves per-mode canned responses and records requested modes.

    ``responses`` maps a mode key → a Routes payload dict (or an int status to
    fail). Transit is split into "TRANSIT" (any-mode) and "TRANSIT_RAIL"
    (rail_only, detected via transitPreferences in the body).
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = body["travelMode"]
        key = mode
        if mode == "TRANSIT" and "transitPreferences" in body:
            key = "TRANSIT_RAIL"
        self.requested.append(key)
        payload = self.responses.get(key)
        if payload is None:
            return httpx.Response(200, text=json.dumps({"routes": []}))
        if isinstance(payload, int):
            return httpx.Response(payload, text="{}")
        return httpx.Response(200, text=json.dumps(payload))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _route(minutes: float, distance_km: float = 5.0, steps: list | None = None):
    """A minimal Routes payload for one route with given total + optional steps."""
    route: dict = {
        "duration": f"{int(minutes * 60)}s",
        "distanceMeters": int(distance_km * 1000),
    }
    if steps is not None:
        route["legs"] = [{"steps": steps}]
    return {"routes": [route]}


def _walk_step(minutes: float, distance_km: float):
    return {
        "travelMode": "WALK",
        "staticDuration": f"{int(minutes * 60)}s",
        "distanceMeters": int(distance_km * 1000),
    }


def _rail_step(minutes: float, distance_km: float, line: str = "N"):
    return {
        "travelMode": "TRANSIT",
        "staticDuration": f"{int(minutes * 60)}s",
        "distanceMeters": int(distance_km * 1000),
        "transitDetails": {"transitLine": {"nameShort": line}},
    }


BIKE_ON = CommutePrefs(min_bike_walk_minutes=12, max_bike_distance_km=10)
BIKE_OFF = CommutePrefs(min_bike_walk_minutes=12, max_bike_distance_km=None)


# --- duration parsing ----------------------------------------------------


def test_duration_to_minutes():
    assert routing._duration_to_minutes("600s") == 10.0
    assert routing._duration_to_minutes("90s") == 1.5
    assert routing._duration_to_minutes(None) == 0.0


# --- three strategies + fastest -----------------------------------------


def test_all_three_strategies_and_best():
    transport = RecordingTransport({
        "TRANSIT": _route(50),                     # no_bike = 50
        "BICYCLE": _route(35, distance_km=8),      # bike_only = 35 (fastest)
        "TRANSIT_RAIL": _route(45, steps=[         # bike_hybrid line-haul
            _walk_step(5, 0.4),                    # short → stays walk (≤12)
            _rail_step(30, 12),
            _walk_step(20, 4.0),                   # long → biked (4km/15kmh≈16min)
        ]),
    })
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_ON, client=transport.client())

    assert plan.strategies["no_bike"] == 50
    assert plan.strategies["bike_only"] == 35
    # hybrid: 5 (walk) + 30 (rail) + 16 (biked) = 51
    assert plan.strategies["bike_hybrid"] == pytest.approx(51, abs=0.6)
    assert plan.best_mode == "bike_only" and plan.best_minutes == 35
    # detail carried for review
    assert "bike_hybrid" in plan.detail and len(plan.detail["bike_hybrid"]) == 3


# --- the null/0-disables rule: NO bike API call at all -------------------


def test_bike_disabled_makes_no_bike_requests():
    transport = RecordingTransport({"TRANSIT": _route(40)})
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_OFF, client=transport.client())

    # Only the any-mode transit call was made — no BICYCLE, no rail-hybrid.
    assert transport.requested == ["TRANSIT"]
    assert "BICYCLE" not in transport.requested
    assert "TRANSIT_RAIL" not in transport.requested
    assert plan.strategies == {"no_bike": 40}
    assert plan.best_mode == "no_bike"


# --- walk gate + max distance in the hybrid substitution -----------------


def test_short_connector_stays_walk_long_connector_bikes():
    # Two walk connectors: 5min/0.3km (short, under gate → walk) and
    # 25min/5km (long, over gate, within max → bike ≈ 20min).
    transport = RecordingTransport({
        "TRANSIT": _route(999),  # make no_bike lose so best = hybrid
        "BICYCLE": _route(999, distance_km=8),
        "TRANSIT_RAIL": _route(60, steps=[
            _walk_step(5, 0.3),
            _rail_step(20, 10),
            _walk_step(25, 5.0),
        ]),
    })
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_ON, client=transport.client())
    # 5 (walk kept) + 20 (rail) + 20 (5km biked) = 45
    assert plan.strategies["bike_hybrid"] == pytest.approx(45, abs=0.6)


def test_connector_over_max_distance_stays_walk():
    # A 12km walk connector exceeds max_bike_distance_km=10 → must NOT be biked.
    transport = RecordingTransport({
        "TRANSIT": _route(999),
        "BICYCLE": _route(999, distance_km=8),
        "TRANSIT_RAIL": _route(90, steps=[
            _rail_step(30, 20),
            _walk_step(60, 12.0),  # 12km > 10km max → keep the 60-min walk
        ]),
    })
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_ON, client=transport.client())
    assert plan.strategies["bike_hybrid"] == pytest.approx(90, abs=0.6)


def test_bike_only_rejected_when_over_max_distance():
    # A door-to-door bike of 15km exceeds the 10km cap → bike_only unavailable.
    transport = RecordingTransport({
        "TRANSIT": _route(50),
        "BICYCLE": _route(40, distance_km=15),
        "TRANSIT_RAIL": _route(55, steps=[_rail_step(55, 20)]),
    })
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_ON, client=transport.client())
    assert plan.strategies["bike_only"] is None
    assert plan.best_mode == "no_bike"  # 50 beats hybrid 55


# --- fail-soft -----------------------------------------------------------


def test_one_mode_failing_drops_only_that_strategy():
    transport = RecordingTransport({
        "TRANSIT": 500,                 # no_bike fails
        "BICYCLE": _route(30, distance_km=6),
        "TRANSIT_RAIL": _route(45, steps=[_rail_step(45, 18)]),
    })
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_ON, client=transport.client())
    assert plan.strategies["no_bike"] is None
    assert plan.strategies["bike_only"] == 30
    assert plan.best_mode == "bike_only"


def test_all_modes_failing_leaves_no_best():
    transport = RecordingTransport({"TRANSIT": 500, "BICYCLE": 500, "TRANSIT_RAIL": 500})
    plan = routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_ON, client=transport.client())
    assert plan.best_minutes is None and plan.best_mode is None


# --- SECURITY: home coords never logged ----------------------------------


def test_home_coords_never_logged(caplog):
    transport = RecordingTransport({"TRANSIT": 500})  # force the warning path
    with caplog.at_level(logging.WARNING, logger="jobscout.routing"):
        routing.plan_commute(HOME, OFFICE, api_key=KEY, prefs=BIKE_OFF, client=transport.client())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert str(HOME.lat) not in logged and str(HOME.lon) not in logged
    assert "2.1657" not in logged and "48.8009" not in logged


def test_google_error_message_surfaced_but_no_coords(caplog):
    # A realistic 403 body (Routes API disabled) must appear in the log so a
    # misconfig is self-diagnosing — while the request coordinates never do.
    error_body = {
        "error": {
            "code": 403,
            "message": "Routes API has not been used in project 42 before or it "
                       "is disabled. Enable it by visiting ... then retry.",
            "status": "PERMISSION_DENIED",
        }
    }

    def handler(request):
        return httpx.Response(403, text=json.dumps(error_body))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="jobscout.routing"):
        routing.compute_route(HOME, OFFICE, mode=routing.DRIVE, api_key=KEY,
                              departure=routing.next_weekday_morning(), client=client)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    # Google's diagnostic is present (status code + its message)…
    assert "HTTP 403" in logged
    assert "Routes API" in logged and "disabled" in logged
    # …but the coordinates are not.
    assert "48.8009" not in logged and "2.1657" not in logged
    assert "48.8918" not in logged  # office lat either


# --- departure time ------------------------------------------------------


def test_next_weekday_morning_skips_weekend():
    from datetime import datetime, timedelta, timezone
    paris = timezone(timedelta(hours=2))
    saturday = datetime(2026, 7, 11, 10, 0, tzinfo=paris)  # a Saturday
    result = routing.next_weekday_morning(now=saturday)
    assert result.astimezone(paris).weekday() == 0  # Monday
    assert result.astimezone(paris).hour == 9

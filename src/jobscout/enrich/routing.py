"""Commute routing via Google Routes, composed into the owner's three strategies.

Two layers:

  1. ``compute_route`` — a thin wrapper over Google Routes' ``computeRoutes``
     endpoint for one (origin, destination, mode) request, returning total
     minutes + distance, and — for transit — the per-*step* breakdown we need to
     substitute bike legs (below). One provider, one key, does DRIVE
     (traffic-aware), TRANSIT (rail-restricted or any-mode), BICYCLE, and WALK.

  2. ``plan_commute`` — composes the three named strategies the owner reviews:
       * ``bike_only``   — door-to-door bike.
       * ``bike_hybrid`` — rail/RER transit, with slow WALK connector steps
                           swapped for bike when they clear the walk gate and sit
                           within the max bike distance. This is how the owner
                           actually commutes (scooter last-mile off a mainline
                           station).
       * ``no_bike``     — plain any-mode transit.
     and picks the fastest as the offer's headline ``commute_minutes``.

Why compose in our code rather than ask Google for "transit + my scooter":
no routing API models personal micromobility as transit access/egress. So we
take Google's rail itinerary, keep the line-haul, and re-time the walk connectors
at bike speed where it's worth it. This is deliberately an *estimate* (we bike
between a step's own endpoints rather than re-optimizing which station to use) —
good enough is the explicit goal.

SECURITY (CLAUDE.md invariant): the home coordinates are the ``origin`` of every
request and must reach *only* the Google request body — never a log line, never
an LLM prompt, never the digest. This module logs durations/distances and step
*modes*, never coordinates. Downstream stores commute *minutes*, not the origin.

The null/0-disables bike bounds arrive as a ``config.CommutePrefs``: when
``bike_enabled`` is False we never build a bike request at all (no wasted call),
per the owner's explicit requirement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from jobscout.config import CommutePrefs

logger = logging.getLogger("jobscout.routing")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Google Routes travel modes we use.
DRIVE = "DRIVE"
TRANSIT = "TRANSIT"
BICYCLE = "BICYCLE"
WALK = "WALK"

# Rail-only transit filter for the bike-hybrid line-haul: trains + RER +
# regional/long-distance rail, explicitly excluding SUBWAY (metro) and BUS.
RAIL_MODES = ["TRAIN", "RAIL", "LIGHT_RAIL"]

# Rough Paris-region bike speed for turning a walk-step's *distance* into a bike
# time when Google gives us a walk duration but we'd rather bike it. Calibrated
# to the owner's real data (2.1 km ≈ 8 min on an upgraded e-scooter ≈ Google's
# bike estimate) → ~15 km/h effective incl. lights. Used only as a fallback when
# a per-step bike routing call isn't made; the primary path routes the bike leg.
_BIKE_KMH = 15.0


@dataclass(frozen=True)
class RouteLeg:
    """One step of an itinerary: its travel mode, minutes, and distance (km).

    ``mode`` is normalized to our vocabulary (WALK/TRANSIT/DRIVE/BICYCLE). For a
    transit step ``line`` names the rail line if Google reported it (audit only).
    """

    mode: str
    minutes: float
    distance_km: float
    line: str | None = None


@dataclass(frozen=True)
class RouteResult:
    """A computed route: total minutes, total distance, and its legs."""

    minutes: float
    distance_km: float
    legs: tuple[RouteLeg, ...] = ()


@dataclass(frozen=True)
class Coordinates:
    """A WGS84 point. Never logged (may be the home location)."""

    lat: float
    lon: float


@dataclass
class CommutePlan:
    """The three strategies for one offer + the chosen headline commute.

    ``strategies`` maps name → minutes (None when that strategy couldn't be
    computed / was disabled). ``best_minutes``/``best_mode`` are the fastest
    available; both None if nothing computed. ``detail`` carries the per-leg
    breakdown for the digest/review, keyed by strategy name.
    """

    strategies: dict[str, float | None] = field(default_factory=dict)
    best_minutes: float | None = None
    best_mode: str | None = None
    detail: dict[str, list[dict]] = field(default_factory=dict)

    def as_json_dict(self) -> dict:
        """Plain dict for the ``commute_strategies`` JSON column."""
        return {
            "best_minutes": self.best_minutes,
            "best_mode": self.best_mode,
            "strategies": self.strategies,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# Departure time
# --------------------------------------------------------------------------


def next_weekday_morning(
    hour: int = 9, *, now: datetime | None = None
) -> datetime:
    """Return the next weekday at ``hour``:00 Europe/Paris, as a UTC datetime.

    A single representative rush-hour departure (the plan's decision): traffic-
    aware driving and timetable-based transit both need a concrete future time.
    We compute in Paris local wall-clock then convert to UTC for the RFC3339
    ``departureTime``. Uses a fixed +02:00 offset (Paris summer); good enough for
    a commute estimate and avoids a tzdata dependency.
    """
    paris = timezone(timedelta(hours=2))
    now = now or datetime.now(paris)
    now = now.astimezone(paris)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    # If today's slot has passed (or it's a weekend), roll forward to the next
    # weekday morning.
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    """RFC3339 / ISO-8601 with a trailing Z, as Google Routes expects."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Google Routes wrapper
# --------------------------------------------------------------------------

_FIELD_MASK = (
    "routes.duration,routes.distanceMeters,"
    "routes.legs.steps.travelMode,routes.legs.steps.staticDuration,"
    "routes.legs.steps.distanceMeters,routes.legs.steps.transitDetails"
)


def _duration_to_minutes(value: object) -> float:
    """Google durations are strings like ``"1234s"``; convert to minutes."""
    if value is None:
        return 0.0
    text = str(value).rstrip("s")
    try:
        return round(float(text) / 60.0, 1)
    except ValueError:
        return 0.0


def _parse_steps(route: dict) -> tuple[RouteLeg, ...]:
    """Flatten a route's legs→steps into our RouteLeg list.

    Google nests ``legs[].steps[]``; each step has ``travelMode`` (WALK/TRANSIT/
    …), ``staticDuration``, ``distanceMeters``, and — for transit — a
    ``transitDetails`` with the line name. We keep every step so ``bike_hybrid``
    can find the WALK connectors to re-time.
    """
    legs_out: list[RouteLeg] = []
    for leg in route.get("legs", []) or []:
        for step in leg.get("steps", []) or []:
            mode = step.get("travelMode") or "UNKNOWN"
            minutes = _duration_to_minutes(step.get("staticDuration"))
            distance_km = round((step.get("distanceMeters") or 0) / 1000.0, 3)
            line = None
            details = step.get("transitDetails") or {}
            line_info = (details.get("transitLine") or {})
            line = line_info.get("nameShort") or line_info.get("name")
            legs_out.append(RouteLeg(mode=mode, minutes=minutes, distance_km=distance_km, line=line))
    return tuple(legs_out)


def _safe_error(exc: Exception) -> str:
    """A coordinate-free, log-safe summary of a routing failure.

    For an HTTP status error, pull Google's ``error.message``/``error.status``
    (or ``status_code`` if the body isn't the expected JSON) — these describe the
    API/quota/billing state and never contain the request coordinates. For a
    transport error with no response (timeout, connect refused), fall back to the
    exception type. This is what lets a "Routes API is disabled" 403 show up in
    the logs directly, instead of an opaque ``HTTPStatusError``.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            error = response.json().get("error", {})
            message = error.get("message") or error.get("status")
            if message:
                # Collapse to one line; cap length so a verbose body can't flood
                # the log. Contains no coordinates (those were request-only).
                return f"HTTP {response.status_code}: {' '.join(str(message).split())[:300]}"
        except (ValueError, AttributeError):
            pass
        return f"HTTP {response.status_code}"
    return type(exc).__name__


def compute_route(
    origin: Coordinates,
    destination: Coordinates,
    *,
    mode: str,
    api_key: str,
    departure: datetime,
    rail_only: bool = False,
    client: httpx.Client | None = None,
    timeout_seconds: float = 20.0,
) -> RouteResult | None:
    """Compute one route via Google Routes; return minutes/distance/legs or None.

    ``mode`` is one of DRIVE/TRANSIT/BICYCLE/WALK. DRIVE requests are
    traffic-aware at ``departure``. ``rail_only`` (transit only) restricts to
    trains/RER, excluding metro and bus. Fail-soft: any transport/HTTP/parse
    error, or no route, returns ``None`` (the caller drops that strategy) — a
    routing failure must never crash the batch.

    SECURITY: ``origin`` (possibly the home location) goes only into the request
    body here. Nothing in this function logs coordinates.
    """
    body: dict[str, object] = {
        "origin": {"location": {"latLng": {"latitude": origin.lat, "longitude": origin.lon}}},
        "destination": {
            "location": {"latLng": {"latitude": destination.lat, "longitude": destination.lon}}
        },
        "travelMode": mode,
        "departureTime": _rfc3339(departure),
    }
    if mode == DRIVE:
        body["routingPreference"] = "TRAFFIC_AWARE"
    if mode == TRANSIT and rail_only:
        body["transitPreferences"] = {"allowedTravelModes": RAIL_MODES}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout_seconds)
    try:
        resp = client.post(ROUTES_URL, json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Log the mode + a coordinate-free error summary. Google's error body
        # (status code + message/status like "Routes API ... is disabled" or a
        # quota/billing note) is API-config info only — it never echoes the
        # request's coordinates — so surfacing it makes a misconfig
        # self-diagnosing from the logs, without breaking the home-coords
        # invariant. Transport errors with no response fall back to the type.
        logger.warning("routing failed (mode=%s): %s", mode, _safe_error(exc))
        return None
    finally:
        if owns_client:
            client.close()

    routes = payload.get("routes") or []
    if not routes:
        return None
    route = routes[0]
    return RouteResult(
        minutes=_duration_to_minutes(route.get("duration")),
        distance_km=round((route.get("distanceMeters") or 0) / 1000.0, 3),
        legs=_parse_steps(route),
    )


# --------------------------------------------------------------------------
# Strategy composition
# --------------------------------------------------------------------------


def _hybridize(transit: RouteResult, prefs: CommutePrefs) -> float:
    """Re-time a rail itinerary's WALK connectors as bike where it's worth it.

    For each WALK step: keep it a walk when there's a walk gate and the step is
    at/under it (short hop — not worth the scooter); otherwise, if the step is
    within the max bike distance, bike it (distance ÷ bike speed); if it's over
    the max distance, leave it as the (long) walk — we won't bike that far and
    have no better connector. Non-walk (rail) steps are kept as-is. Returns the
    recomputed total minutes.

    Estimating the bike time from the step's own distance (rather than a fresh
    routing call per connector) is the deliberate "good estimate" simplification
    — it avoids N extra API calls per offer for marginal accuracy.
    """
    total = 0.0
    for leg in transit.legs:
        if leg.mode != WALK:
            total += leg.minutes
            continue
        # A walk connector — decide walk vs. bike.
        under_gate = prefs.has_walk_gate and leg.minutes <= prefs.min_bike_walk_minutes
        too_far_to_bike = (
            prefs.max_bike_distance_km is not None
            and leg.distance_km > prefs.max_bike_distance_km
        )
        if under_gate or too_far_to_bike:
            total += leg.minutes  # keep walking
        else:
            total += round(leg.distance_km / _BIKE_KMH * 60.0, 1)  # bike it
    return round(total, 1)


def plan_commute(
    home: Coordinates,
    office: Coordinates,
    *,
    api_key: str,
    prefs: CommutePrefs,
    departure: datetime | None = None,
    client: httpx.Client | None = None,
) -> CommutePlan:
    """Compute the three commute strategies for one offer and pick the fastest.

    - ``no_bike``: any-mode transit (metro/bus/walk).
    - ``bike_only``: door-to-door bike — **only if biking is enabled**.
    - ``bike_hybrid``: rail-only transit with walk→bike connector substitution —
      **only if biking is enabled** (else there's nothing to hybridize).

    Respects the null/0-disables rule *before* issuing calls: when
    ``prefs.bike_enabled`` is False, no BICYCLE or rail-hybrid request is made.
    Each strategy is independently fail-soft (a None route → that strategy is
    absent, not a crash). ``best_*`` is the min over whatever computed.
    """
    departure = departure or next_weekday_morning()
    plan = CommutePlan()

    def _record(name: str, route: RouteResult | None, minutes: float | None) -> None:
        plan.strategies[name] = minutes
        if route is not None:
            plan.detail[name] = [
                {"mode": leg.mode, "minutes": leg.minutes, "distance_km": leg.distance_km,
                 "line": leg.line}
                for leg in route.legs
            ]

    # no_bike — plain any-mode transit (always attempted).
    transit_any = compute_route(
        home, office, mode=TRANSIT, api_key=api_key, departure=departure, client=client
    )
    _record("no_bike", transit_any, transit_any.minutes if transit_any else None)

    if prefs.bike_enabled:
        # bike_only — door-to-door bike.
        bike = compute_route(
            home, office, mode=BICYCLE, api_key=api_key, departure=departure, client=client
        )
        # Guard: a single bike route longer than the max distance isn't a real
        # bike_only option (matches the per-leg rule for the whole trip).
        if bike is not None and (
            prefs.max_bike_distance_km is None
            or bike.distance_km <= prefs.max_bike_distance_km
        ):
            _record("bike_only", bike, bike.minutes)
        else:
            plan.strategies["bike_only"] = None

        # bike_hybrid — rail-only line-haul with walk→bike connectors.
        transit_rail = compute_route(
            home, office, mode=TRANSIT, api_key=api_key, departure=departure,
            rail_only=True, client=client,
        )
        if transit_rail is not None:
            hybrid_minutes = _hybridize(transit_rail, prefs)
            _record("bike_hybrid", transit_rail, hybrid_minutes)
        else:
            plan.strategies["bike_hybrid"] = None

    # Pick the fastest available strategy.
    available = {k: v for k, v in plan.strategies.items() if v is not None}
    if available:
        plan.best_mode = min(available, key=available.get)
        plan.best_minutes = available[plan.best_mode]
    return plan

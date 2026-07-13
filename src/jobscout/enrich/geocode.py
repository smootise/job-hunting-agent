"""Geocoding via Base Adresse Nationale (BAN) — free-text address → coordinates.

BAN (``api-adresse.data.gouv.fr``) is the official French address base: free,
keyless, and far more accurate for FR addresses than a generic geocoder. The
brief mandates it for exactly this reason, and — crucially — as the *validation*
gate: every resolved address, whatever produced it, must geocode here and land
in the expected region, or it is not trusted. That region check is what stops a
wrong address (a bad parse now, a hallucinated one from the Phase 3 research
agent later) from feeding a nonsense commute into scoring.

This module is deliberately thin and pure-ish: one ``geocode`` call over BAN's
GeoJSON ``/search`` endpoint, plus a region predicate. The HTTP client is
injectable so tests run fully offline.

BAN response shape we rely on (GeoJSON):
  features[].geometry.coordinates = [lon, lat]   (note the order!)
  features[].properties = {label, city, postcode, context, score, type, ...}
  properties.context = "78, Yvelines, Île-de-France"  ← region lives here.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

BAN_SEARCH_URL = "https://api-adresse.data.gouv.fr/search/"

# The eight Île-de-France department codes. A BAN result's postcode (or the
# leading token of its `context`) begins with one of these iff it's in-region.
# Kept as a frozenset of the two-digit prefixes; 75/77/78/91/92/93/94/95.
IDF_DEPARTMENTS: frozenset[str] = frozenset(
    {"75", "77", "78", "91", "92", "93", "94", "95"}
)


@dataclass(frozen=True)
class GeocodeResult:
    """One geocoded address. ``score`` is BAN's 0–1 match confidence."""

    address: str          # BAN's canonical label, e.g. "3 Avenue ... 78220 Viroflay"
    lat: float
    lon: float
    city: str | None
    postcode: str | None
    score: float
    department: str | None  # two-digit code, e.g. "78" — for the region check

    @property
    def in_idf(self) -> bool:
        """True iff the result sits in an Île-de-France department."""
        return self.department in IDF_DEPARTMENTS


def _department_of(postcode: str | None, context: str | None) -> str | None:
    """Extract the two-digit department code from a BAN result.

    Prefer the postcode's first two digits (unambiguous); fall back to the
    leading token of ``context`` ("78, Yvelines, Île-de-France"). Corsica's
    "2A"/"2B" aren't IDF so the plain two-char slice is fine here.
    """
    if postcode and len(postcode) >= 2:
        return postcode[:2]
    if context:
        head = context.split(",", 1)[0].strip()
        if len(head) >= 2:
            return head[:2]
    return None


def geocode(
    query: str,
    *,
    postcode: str | None = None,
    limit: int = 1,
    client: httpx.Client | None = None,
    timeout_seconds: float = 15.0,
) -> GeocodeResult | None:
    """Geocode ``query`` via BAN; return the best match or ``None``.

    ``postcode`` biases BAN toward the right commune when the query is just a
    city name (BAN accepts a ``postcode`` filter). Returns ``None`` on an empty
    query, no features, or any transport error — geocoding is best-effort and
    never raises into the caller (a failed geocode falls through to the caller's
    next fallback, per the brief's resolution chain). The caller decides whether
    an out-of-region result (``.in_idf is False``) is usable.
    """
    if not query or not query.strip():
        return None

    params: dict[str, object] = {"q": query.strip(), "limit": limit, "autocomplete": 0}
    if postcode:
        params["postcode"] = postcode

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout_seconds)
    try:
        resp = client.get(BAN_SEARCH_URL, params=params)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns_client:
            client.close()

    features = body.get("features") or []
    if not features:
        return None

    best = features[0]
    geometry = best.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    if len(coords) < 2:
        return None
    lon, lat = float(coords[0]), float(coords[1])  # BAN order is [lon, lat].

    props = best.get("properties") or {}
    result_postcode = props.get("postcode")
    department = _department_of(result_postcode, props.get("context"))

    return GeocodeResult(
        address=props.get("label") or query.strip(),
        lat=lat,
        lon=lon,
        city=props.get("city"),
        postcode=result_postcode,
        score=float(props.get("score", 0.0)),
        department=department,
    )

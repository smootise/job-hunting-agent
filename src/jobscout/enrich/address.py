"""Resolve an offer's office address to coordinates for commute routing.

The deterministic resolution chain (the online research *agent* is Phase 3):

  1. **Best stated location** — take the offer's ``location`` (WTTJ/FT give a
     city, sometimes with region), optionally sharpened by a street address
     found in the ``description`` prose, and geocode it via BAN.
  2. **City-centroid fallback** — if we only had a city (or the precise query
     didn't resolve), geocode the city alone → an approximate point. Marked
     ``address_source='approximate'`` / ``confidence='low'`` so downstream treats
     its commute as an estimate for review, never a hard-reject (the brief's
     cardinal rule: a wrong inferred address must never silently kill an offer).

Two short-circuits before any geocoding:
  * **Fully-remote offers** need no address — we return a ``remote`` resolution
    (the caller sets ``commute_minutes=0`` and skips routing entirely).
  * **No usable location text at all** → an ``unresolved`` result (the caller
    leaves the row unenriched, fail-soft).

``address_source`` values here: ``posting`` (a street address we found in prose),
``wttj``/``france_travail``/``linkedin_email`` (the source's stated city resolved
cleanly), ``approximate`` (city centroid), plus the two non-address outcomes
``remote`` and ``unresolved``. ``registry``/``inferred`` are Phase 3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from jobscout.enrich import geocode
from jobscout.models import JobRecord
from jobscout.pipeline.filters import classify_remote_policy


@dataclass(frozen=True)
class ResolvedAddress:
    """The outcome of resolving one offer's office location.

    For ``remote``/``unresolved`` the coordinate fields are ``None``. For a
    resolved address they carry the geocoded point; ``in_idf`` echoes the BAN
    region check so the caller can flag out-of-region results.
    """

    source: str  # posting | wttj | france_travail | linkedin_email | approximate | remote | needs_address | unresolved
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    confidence: str | None = None  # high | medium | low | None
    in_idf: bool | None = None

    @property
    def is_remote(self) -> bool:
        return self.source == "remote"

    @property
    def needs_address(self) -> bool:
        """A location too vague to route (bare 'Paris') — a Phase 3 worklist item.

        Distinct from ``unresolved`` (no location text at all): here we *have* a
        city, but it's a large commune whose centroid is a useless commute proxy,
        so we deliberately skip routing and leave it for the address-research
        agent to sharpen to a real street/arrondissement."""
        return self.source == "needs_address"

    @property
    def is_resolved(self) -> bool:
        return self.lat is not None and self.lon is not None


# A French street address inside prose: a house number + a street-type word.
# Deliberately conservative — a false street match sends us to a wrong precise
# point, whereas missing one just falls back to the (safe) city centroid.
_STREET_RE = re.compile(
    r"\b\d{1,4}\s+(?:bis\s+|ter\s+)?"
    r"(?:rue|avenue|av\.?|boulevard|bd\.?|place|impasse|allée|allee|quai|"
    r"chemin|route|cours|passage)\b[^,;\n]{0,60}",
    re.IGNORECASE,
)

# Trailing remote/region tag WTTJ appends, e.g. "Paris, Île-de-France (remote: …)".
_LOCATION_TAIL_RE = re.compile(r"\s*\(remote:.*?\)\s*$", re.IGNORECASE)


def _clean_location(location: str | None) -> str | None:
    """Strip the "(remote: …)" tag the WTTJ adapter appends to a location."""
    if not location:
        return None
    cleaned = _LOCATION_TAIL_RE.sub("", location).strip()
    return cleaned or None


def _city_from_location(location: str | None) -> str | None:
    """The city portion of a "City, Region" location string (first segment)."""
    cleaned = _clean_location(location)
    if not cleaned:
        return None
    return cleaned.split(",", 1)[0].strip() or None


def _street_from_description(description: str | None) -> str | None:
    """A street address found in the posting prose, or None."""
    if not description:
        return None
    match = _STREET_RE.search(description)
    return match.group(0).strip() if match else None


# Bare "Paris" with no finer specificity. Paris spans ~105 km² and 20
# arrondissements, so a centroid commute is meaningless — better to flag it for
# the Phase 3 address agent than to store a confident-looking wrong number. We
# match ONLY the bare city: "Paris 11e", "Paris 75011", "Paris 15" all carry real
# arrondissement/postcode signal and are kept (arrondissement geocoding is
# useful). A trailing digit or a "e"/"er"/"ème" ordinal means it's specific.
_BARE_PARIS_RE = re.compile(r"^paris\b(?!\s*\d)(?!\s*(?:e|er|[eè]me)\b)$", re.IGNORECASE)


def _is_too_vague_to_route(city: str | None) -> bool:
    """True for a bare 'Paris' (no arrondissement/postcode) — skip routing.

    Deliberately narrow (owner's decision: 'Paris' only, not every big city): a
    bare-Paris centroid is the one case common enough in the data and vague
    enough to be worse than useless. Anything with an arrondissement or postcode
    is specific enough to route.
    """
    if not city:
        return False
    return _BARE_PARIS_RE.match(city.strip()) is not None


def resolve_address(
    record: JobRecord,
    *,
    client: httpx.Client | None = None,
) -> ResolvedAddress:
    """Resolve one offer's office location to coordinates (or remote/unresolved).

    Order: remote short-circuit → precise street (from prose) → bare-Paris
    guard → stated city → city-centroid fallback. Every geocode is region-checked;
    an out-of-IDF hit is returned (``in_idf=False``) rather than dropped, so the
    caller can flag it — but we prefer an in-region city centroid over an
    out-of-region precise hit when both exist. Fail-soft throughout: a geocode
    miss falls to the next step, and exhausting the chain yields ``unresolved``.
    """
    # 1. Fully-remote → no address needed.
    if classify_remote_policy(record) == "remote":
        return ResolvedAddress(source="remote")

    city = _city_from_location(record.location)
    street = _street_from_description(record.description)

    # 2. Precise street address from prose (highest precision), biased by city.
    # A real street wins even in Paris — the bare-Paris guard below only fires
    # when all we have is the vague city.
    if street:
        query = f"{street}, {city}" if city else street
        hit = geocode.geocode(query, client=client)
        if hit is not None and hit.in_idf:
            return ResolvedAddress(
                source="posting", address=hit.address, lat=hit.lat, lon=hit.lon,
                city=hit.city or city, confidence="high", in_idf=True,
            )

    # 3. Bare "Paris" with no street/arrondissement → too vague to route. Skip
    # geocoding entirely and flag for the Phase 3 address agent (a city-centroid
    # commute to "Paris" is a confident-looking wrong number).
    if _is_too_vague_to_route(city):
        return ResolvedAddress(source="needs_address", city=city)

    # 4. Stated city resolved cleanly → tag with the source it came from.
    if city:
        hit = geocode.geocode(city, client=client)
        if hit is not None:
            # A city geocode is a centroid-ish point: medium confidence when it's
            # the exact stated city and in-region, else the approximate fallback.
            if hit.in_idf:
                return ResolvedAddress(
                    source=record.source, address=hit.address, lat=hit.lat,
                    lon=hit.lon, city=hit.city or city, confidence="medium",
                    in_idf=True,
                )
            # Out-of-region: keep it but flag as approximate/low so it can never
            # hard-reject and is surfaced for review.
            return ResolvedAddress(
                source="approximate", address=hit.address, lat=hit.lat,
                lon=hit.lon, city=hit.city or city, confidence="low",
                in_idf=False,
            )

    # 5. Nothing usable.
    return ResolvedAddress(source="unresolved")

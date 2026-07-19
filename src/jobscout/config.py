"""Loads and validates preferences.yaml.

preferences.yaml is gitignored (it contains the owner's home location) and
is the single source of truth for hard filters and the scoring rubric. This
module's job is just to load it into a typed shape — the *interpretation*
of each field (e.g. the commute-filter null-means-no-filter trap, the
whole-token seniority matching) lives in the pipeline stage that consumes
it, not here. Keeping that logic close to its usage is what CLAUDE.md's
"How to interpret preferences.yaml" section is documenting against.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PREFERENCES_PATH = Path("preferences.yaml")
DEFAULT_ENV_PATH = Path(".env")


def load_preferences(path: Path = DEFAULT_PREFERENCES_PATH) -> dict[str, Any]:
    """Read preferences.yaml and return it as a plain dict.

    Deliberately returns a dict rather than a frozen dataclass for now:
    the schema is still settling (see preferences.example.yaml), and a
    thin loader keeps this file small while the pipeline stages are built.
    Revisit with typed models once the schema stabilizes.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy preferences.example.yaml to "
            f"{path} and fill in your own values (this file is gitignored)."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------
# Secrets (.env) — credentials for the sources that need them
# --------------------------------------------------------------------------
#
# We hand-roll a tiny .env reader instead of pulling in python-dotenv: the
# file has ~5 keys, the format is trivial (KEY=VALUE lines), and avoiding a
# dependency for that keeps the surface small. Values already present in the
# real process environment win over the file, so CI or a shell export can
# override without editing .env. Secrets are read here and passed explicitly
# to adapters — they are never logged and never enter an LLM prompt
# (CLAUDE.md's Security invariants).


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Parse a .env file into a dict, merged under the real environment.

    Missing file is not an error — a run using only WTTJ (no credentials)
    must work with no .env at all. Blank lines and `#` comments are skipped;
    surrounding quotes on a value are stripped. The live `os.environ` takes
    precedence over file values.
    """
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    # Real environment overrides the file.
    for key in list(values):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


@dataclass(frozen=True)
class FranceTravailCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class ImapCredentials:
    host: str
    user: str
    app_password: str


def _require(env: dict[str, str], *keys: str) -> list[str]:
    """Return the values for `keys`, or raise a clear, actionable error.

    The error names exactly which keys are missing and points at .env, so a
    half-configured source fails loudly at startup rather than deep inside an
    HTTP call with an opaque 401.
    """
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing required credential(s) {', '.join(missing)}. "
            f"Set them in .env (copy from .env.example)."
        )
    return [env[k] for k in keys]


def france_travail_credentials(
    env: dict[str, str] | None = None,
) -> FranceTravailCredentials:
    """FT client id/secret, or a clear error if not configured."""
    env = env if env is not None else load_env()
    client_id, client_secret = _require(
        env, "FRANCE_TRAVAIL_ID", "FRANCE_TRAVAIL_SECRET"
    )
    return FranceTravailCredentials(client_id, client_secret)


def imap_credentials(env: dict[str, str] | None = None) -> ImapCredentials:
    """IMAP host/user/app-password, or a clear error if not configured."""
    env = env if env is not None else load_env()
    host, user, password = _require(
        env, "IMAP_HOST", "IMAP_USER", "IMAP_APP_PASSWORD"
    )
    return ImapCredentials(host, user, password)


def google_routes_key(env: dict[str, str] | None = None) -> str:
    """The Google Routes API key, or a clear error if not configured.

    Same fail-loud pattern as the other credential getters: a run that reaches
    commute enrichment without a key fails at startup with an actionable message
    rather than deep inside an HTTP 403. This is the single external service that
    receives the owner's home coordinates (see docs/enrichment.md)."""
    env = env if env is not None else load_env()
    (key,) = _require(env, "GOOGLE_ROUTES_KEY")
    return key


def searxng_url(env: dict[str, str] | None = None) -> str:
    """The base URL of the self-hosted SearXNG instance, or a clear error.

    Same fail-loud pattern as the other getters: the Phase 3 research agents'
    ``web_search`` tool needs a reachable SearXNG (the owner runs one on their
    LAN, e.g. ``http://192.168.1.63:8085``); a run that reaches an agent without
    it should fail at startup with an actionable message rather than deep inside
    an HTTP call. This is a LAN URL, not a secret, but it's read from ``.env``
    for consistency with every other endpoint/credential. The trailing slash is
    stripped so callers can append ``/search`` uniformly.
    """
    env = env if env is not None else load_env()
    (url,) = _require(env, "SEARXNG_URL")
    return url.rstrip("/")


# --------------------------------------------------------------------------
# Commute-enrichment preferences (home origin + bike tunables)
# --------------------------------------------------------------------------
#
# Like the hard filters, the *interpretation* of these fields lives close to
# their usage — but two pieces are subtle enough to centralize here so every
# consumer reads them identically:
#   * the home origin can be given as an address (geocoded later) OR as pinned
#     lat/lon (skip geocoding); and
#   * the two bike bounds follow the project's null/0-disables convention
#     (CLAUDE.md), so a consumer must never write a bare `if leg > bound`.


@dataclass(frozen=True)
class HomeLocation:
    """The commute origin as configured. Exactly one of (lat & lon) or address
    is authoritative: if ``lat``/``lon`` are set they win and no geocoding is
    needed; otherwise ``address`` is geocoded once by the enrichment stage."""

    label: str | None
    address: str | None
    lat: float | None
    lon: float | None

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class CommutePrefs:
    """The two bike bounds, already interpreted for the null/0-disables rule.

    ``min_bike_walk_minutes``/``max_bike_distance_km`` are ``None`` when the
    guard is disabled (config value absent, null, or 0), so consumers branch on
    ``bike_enabled`` / ``has_walk_gate`` rather than comparing against 0 — the
    trap CLAUDE.md warns about (a bare ``leg > 0`` would bike/skip everything).
    """

    min_bike_walk_minutes: float | None
    max_bike_distance_km: float | None

    @property
    def bike_enabled(self) -> bool:
        """False when biking is disabled outright (``max_bike_distance_km``
        0/null): the ``bike_only``/``bike_hybrid`` strategies are skipped and no
        bike routing call is ever made."""
        return self.max_bike_distance_km is not None

    @property
    def has_walk_gate(self) -> bool:
        """False when there's no lower walk gate (``min_bike_walk_minutes``
        0/null): every leg within the distance bound may bike."""
        return self.min_bike_walk_minutes is not None


def _positive_or_none(value: object) -> float | None:
    """A config number, or None when absent/null/0/negative (guard disabled).

    Centralizes the null/0-disables reading so no consumer re-implements it as a
    bare comparison. Non-numeric or non-positive → None == 'this guard is off'.
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def home_location(prefs: dict[str, Any] | None = None) -> HomeLocation:
    """Read the home origin from ``preferences.yaml``'s ``home:`` block."""
    prefs = prefs if prefs is not None else load_preferences()
    home = (prefs or {}).get("home", {}) or {}
    lat = home.get("lat")
    lon = home.get("lon")
    return HomeLocation(
        label=home.get("label"),
        address=home.get("address"),
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
    )


def commute_prefs(prefs: dict[str, Any] | None = None) -> CommutePrefs:
    """Read the ``commute:`` block, applying the null/0-disables convention."""
    prefs = prefs if prefs is not None else load_preferences()
    commute = (prefs or {}).get("commute", {}) or {}
    return CommutePrefs(
        min_bike_walk_minutes=_positive_or_none(commute.get("min_bike_walk_minutes")),
        max_bike_distance_km=_positive_or_none(commute.get("max_bike_distance_km")),
    )

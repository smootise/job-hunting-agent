"""Fail-fast smoke test for the Google Routes API key + commute wiring.

Run this ONCE after creating your GOOGLE_ROUTES_KEY, before the first real
`jobscout enrich-commute`, so an auth / quota / API-not-enabled problem surfaces
here with a clear message instead of deep inside the pipeline marking every
offer "failed".

It routes your configured home → one known Île-de-France office (La Défense) for
each mode the pipeline uses (driving, transit, bike) and prints the parsed
minutes. Throwaway validation tooling — not part of the jobscout package — but it
routes through the same `jobscout.enrich.routing` code the pipeline uses, so a
green run here means the real stage's provider integration works.

Usage:  uv run python scripts/routes_smoke.py

Privacy note: like the pipeline, this sends your home coordinates only to Google
(for routing) and prints commute *minutes* only — never the coordinates.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from jobscout import config  # noqa: E402
from jobscout.enrich import geocode  # noqa: E402
from jobscout.enrich import routing  # noqa: E402
from jobscout.enrich.routing import Coordinates  # noqa: E402

# A stable, well-known IDF office cluster (La Défense) as the smoke-test target.
OFFICE = Coordinates(lat=48.8918, lon=2.2361)


def _resolve_home() -> Coordinates:
    prefs = config.load_preferences()
    home = config.home_location(prefs)
    if home.has_coords:
        return Coordinates(lat=home.lat, lon=home.lon)
    if home.address:
        hit = geocode.geocode(home.address)
        if hit is not None:
            print(f"  geocoded home address -> dept {hit.department} "
                  f"(in IDF: {hit.in_idf})")
            return Coordinates(lat=hit.lat, lon=hit.lon)
    raise SystemExit(
        "Home not resolvable. Set home.lat/home.lon or a geocodable "
        "home.address in preferences.yaml."
    )


def main() -> None:
    try:
        api_key = config.google_routes_key()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    print("Resolving home origin...")
    home = _resolve_home()

    departure = routing.next_weekday_morning()
    print(f"Departure (representative rush hour): {departure.isoformat()}")
    print("Routing home -> La Defense for each mode:\n")

    any_ok = False
    for label, mode, rail in (
        ("driving (traffic-aware)", routing.DRIVE, False),
        ("transit (any mode)", routing.TRANSIT, False),
        ("transit (rail only)", routing.TRANSIT, True),
        ("bike", routing.BICYCLE, False),
    ):
        result = routing.compute_route(
            home, OFFICE, mode=mode, api_key=api_key,
            departure=departure, rail_only=rail,
        )
        if result is None:
            print(f"  {label:<26} -> no route / error")
        else:
            any_ok = True
            print(f"  {label:<26} -> {result.minutes:.0f} min "
                  f"({result.distance_km:.1f} km, {len(result.legs)} legs)")

    print()
    if any_ok:
        print("[OK] Google Routes is reachable and the key works. "
              "Safe to run `jobscout enrich-commute`.")
    else:
        raise SystemExit(
            "[FAIL] Every mode failed. Check: the key is valid, the 'Routes API' "
            "is ENABLED in the Google Cloud project, and billing is active."
        )


if __name__ == "__main__":
    main()

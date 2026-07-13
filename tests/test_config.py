"""Tests for the commute-enrichment config readers.

Focus on the two subtleties centralized in config.py: the home origin's
"coords win over address" rule, and the null/0-disables convention for the bike
bounds (CLAUDE.md's trap — a bare `leg > bound` must never be written, so the
readers turn absent/null/0 into None == "guard off").
"""

from __future__ import annotations

import pytest

from jobscout import config


@pytest.fixture
def write_prefs(tmp_path):
    def _write(body: str):
        p = tmp_path / "preferences.yaml"
        p.write_text(body, encoding="utf-8")
        return config.load_preferences(p)

    return _write


# --- home_location -------------------------------------------------------


def test_home_location_reads_address(write_prefs):
    prefs = write_prefs('home:\n  label: "Home"\n  address: "1 rue X, 75001 Paris"\n')
    home = config.home_location(prefs)
    assert home.address == "1 rue X, 75001 Paris"
    assert home.label == "Home"
    assert not home.has_coords  # no lat/lon → must geocode the address


def test_home_location_coords_available(write_prefs):
    prefs = write_prefs("home:\n  address: '1 rue X'\n  lat: 48.8\n  lon: 2.35\n")
    home = config.home_location(prefs)
    assert home.has_coords
    assert home.lat == 48.8 and home.lon == 2.35


def test_home_location_missing_block_is_empty(write_prefs):
    home = config.home_location(write_prefs("meta: {}\n"))
    assert home.address is None and not home.has_coords


# --- commute_prefs: the null/0-disables convention -----------------------


def test_commute_bounds_positive_values(write_prefs):
    prefs = write_prefs("commute:\n  min_bike_walk_minutes: 12\n  max_bike_distance_km: 10\n")
    cp = config.commute_prefs(prefs)
    assert cp.min_bike_walk_minutes == 12 and cp.has_walk_gate
    assert cp.max_bike_distance_km == 10 and cp.bike_enabled


@pytest.mark.parametrize("value", ["0", "null", ""])
def test_max_bike_zero_or_null_disables_biking(write_prefs, value):
    # The critical case: 0/null must mean "biking off", NOT "bike anything ≤ 0".
    body = "commute:\n  max_bike_distance_km: " + value + "\n"
    cp = config.commute_prefs(write_prefs(body))
    assert cp.max_bike_distance_km is None
    assert cp.bike_enabled is False


@pytest.mark.parametrize("value", ["0", "null"])
def test_min_walk_zero_or_null_disables_lower_gate(write_prefs, value):
    body = "commute:\n  min_bike_walk_minutes: " + value + "\n  max_bike_distance_km: 10\n"
    cp = config.commute_prefs(write_prefs(body))
    assert cp.min_bike_walk_minutes is None
    assert cp.has_walk_gate is False
    assert cp.bike_enabled is True  # biking still on; only the lower gate is off


def test_commute_missing_block_disables_everything(write_prefs):
    cp = config.commute_prefs(write_prefs("meta: {}\n"))
    assert not cp.bike_enabled and not cp.has_walk_gate


# --- google_routes_key ---------------------------------------------------


def test_google_routes_key_present():
    assert config.google_routes_key({"GOOGLE_ROUTES_KEY": "abc"}) == "abc"


def test_google_routes_key_missing_raises():
    with pytest.raises(RuntimeError, match="GOOGLE_ROUTES_KEY"):
        config.google_routes_key({})

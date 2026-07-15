"""Tests for the deterministic weekly_commute_fit curve.

The single most important test here pins the owner's 12 calibration rankings:
the curve was fitted to them, so a future edit to an anchor that drifts the
calibration must break a test rather than silently re-score every offer. The
rest lock the invariants (unknown → None, monotonic in minutes, the knees).
"""

from __future__ import annotations

import pytest

from jobscout.pipeline.commute_score import _oneway_penalty, weekly_commute_fit

# The owner's 12 ranked scenarios: (one_way_minutes, onsite_days, owner_score).
# See docs/scoring.md. The fit's mean absolute error is ~0.39, so we allow a
# tolerance of 1.5 points per scenario — enough that the fit passes but a real
# miscalibration (a wrong anchor) still trips it.
_CALIBRATION = [
    ("A", 20, 3, 10),
    ("D", 30, 2, 10),
    ("K", 25, 5, 9),
    ("B", 40, 3, 7),
    ("C", 40, 5, 5),
    ("E", 60, 2, 6),
    ("J", 50, 3, 5),
    ("F", 60, 4, 3),
    ("G", 75, 3, 3),
    ("H", 90, 2, 2),
    ("I", 90, 4, 0),
    ("L", 110, 3, 0),
]


@pytest.mark.parametrize("name,one_way,onsite,owner", _CALIBRATION)
def test_matches_owner_calibration(name, one_way, onsite, owner):
    fit = weekly_commute_fit(one_way, onsite)
    assert fit is not None
    assert abs(fit - owner) <= 1.5, f"{name}: fit {fit:.1f} vs owner {owner}"


def test_total_calibration_error_is_small():
    """The aggregate fit should stay tight, not just each point within tolerance."""
    total = sum(
        abs(weekly_commute_fit(ow, d) - owner) for _, ow, d, owner in _CALIBRATION
    )
    assert total / len(_CALIBRATION) <= 0.5


def test_unknown_commute_is_none_not_zero():
    # None means "unknown" — the caller drops the criterion, never scores it 0.
    assert weekly_commute_fit(None, 3) is None
    assert weekly_commute_fit(None, 0) is None


def test_remote_is_max_score():
    # Fully-remote offers are enriched with commute_minutes = 0.
    assert weekly_commute_fit(0.0, 0) == 10.0
    assert weekly_commute_fit(0.0, 3) == 10.0


def test_monotonic_in_minutes_at_fixed_onsite():
    """More one-way minutes never scores higher (weekly + one-way both grow)."""
    prev = 10.1
    for one_way in range(0, 130, 5):
        fit = weekly_commute_fit(float(one_way), 3)
        assert fit <= prev + 1e-9, f"non-monotonic at {one_way} min"
        prev = fit


def test_oneway_penalty_steepens_past_the_knee():
    """The one-way penalty term grows faster past 75 min than below it — the
    rubric's 'differences above 75 min one-way matter much more'. Tested on the
    penalty component in isolation (the weekly-total term is separate)."""
    below_knee = _oneway_penalty(75) - _oneway_penalty(60)
    above_knee = _oneway_penalty(105) - _oneway_penalty(90)
    assert above_knee > below_knee


def test_oneway_penalty_free_below_threshold():
    assert _oneway_penalty(20) == 0.0
    assert _oneway_penalty(25) == 0.0
    assert _oneway_penalty(40) > 0.0


def test_many_short_trips_beat_fewer_long_ones():
    """The K-vs-J calibration: a similar weekly total scores higher when the
    individual leg is shorter (25 min × 5 days vs 50 min × 3 days)."""
    many_short = weekly_commute_fit(25, 5)  # 250 min/week
    fewer_long = weekly_commute_fit(50, 3)  # 300 min/week
    assert many_short > fewer_long


def test_score_clamped_to_range():
    assert weekly_commute_fit(200, 5) == 0.0  # absurd commute floors at 0
    assert 0.0 <= weekly_commute_fit(1, 1) <= 10.0

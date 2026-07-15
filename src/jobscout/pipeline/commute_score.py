"""The deterministic ``weekly_commute_fit`` sub-score.

This is the one scoring criterion the LLM does **not** judge (owner's decision —
see ``docs/architecture.md`` → "Current state & next"). The enrichment stage
already wrote a one-way ``commute_minutes`` to each job row; here we turn that,
plus the onsite-days the LLM inferred from the posting, into a 0–10 sub-score.
Keeping it in Python means a better address or a changed onsite estimate
re-scores for free (``jobscout score --rescore``) with no LLM call.

Why a *two-input* curve instead of the brief's single ``one-way × 2 × onsite``
formula
------------------------------------------------------------------------------
The owner ranked 12 commute scenarios (see the plan / ``docs/scoring.md``). Two
of them settle the design:

  * 90 min one-way × 2 days  = 360 min/week  → scored **2**
  * 40 min one-way × 5 days  = 400 min/week  → scored **5**

The second has a *larger* weekly total yet scored *higher*: a long single leg
hurts beyond what the weekly total alone explains. So the score is
``weekly_component(weekly_total)`` (the burden of total time on the road) minus
an ``oneway_penalty(one_way)`` that only bites past ~40 min and steepens past
~75 min (matching the rubric's "differences above 75 min one-way matter much
more than differences between 20 and 40"). Both terms are piecewise-linear over
named anchor points so the curve is legible and a future change is a conscious,
visible edit — ``tests/test_commute_score.py`` pins the owner's 12 rankings so
tweaking an anchor can't silently drift the calibration.

No I/O here — a pure function over two numbers, like ``filters.py``.
"""

from __future__ import annotations


# (weekly minutes, score) anchors for the base burden term. Interpolated
# linearly between points; flat at the ends. Calibrated to the owner's rankings:
# ~2h/week is still near-ideal, the knee is around a full 8h/week, and past
# ~12h/week (720 min) the commute is a dealbreaker.
_WEEKLY_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.0, 10.0),
    (120.0, 10.0),
    (200.0, 8.8),
    (300.0, 7.0),
    (400.0, 5.6),
    (480.0, 4.0),
    (600.0, 1.6),
    (720.0, 0.2),
    (800.0, 0.0),
)

# One-way length is penalty-free up to this many minutes; a modest penalty then
# grows, so that "many short trips" (e.g. 25 min × 5 days) out-scores "fewer
# long trips" at a similar weekly total (the owner's K-vs-J ranking).
_ONEWAY_FREE_MIN = 25.0
# The "long single leg" knee: penalty grows linearly to ~2.6 by 75 min, then
# steepens (each further 15 min costs another ~1.6) — matching the rubric's
# "differences above 75 min one-way matter much more".
_ONEWAY_KNEE_MIN = 75.0
_PENALTY_AT_KNEE = 2.6
_PENALTY_PER_15_BEYOND_KNEE = 1.6


def _interpolate(x: float, anchors: tuple[tuple[float, float], ...]) -> float:
    """Piecewise-linear interpolation of ``x`` over ``(input, output)`` anchors.

    Clamps flat below the first anchor and above the last — the curve never
    extrapolates past its calibrated range.
    """
    if x <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x <= x1:
            span = x1 - x0
            t = 0.0 if span == 0 else (x - x0) / span
            return y0 + t * (y1 - y0)
    return anchors[-1][1]


def _oneway_penalty(one_way_minutes: float) -> float:
    """The 'long single leg' penalty subtracted from the weekly burden score."""
    if one_way_minutes <= _ONEWAY_FREE_MIN:
        return 0.0
    if one_way_minutes <= _ONEWAY_KNEE_MIN:
        span = _ONEWAY_KNEE_MIN - _ONEWAY_FREE_MIN
        return (one_way_minutes - _ONEWAY_FREE_MIN) / span * _PENALTY_AT_KNEE
    beyond = one_way_minutes - _ONEWAY_KNEE_MIN
    return _PENALTY_AT_KNEE + beyond / 15.0 * _PENALTY_PER_15_BEYOND_KNEE


def weekly_commute_fit(
    commute_minutes: float | None, onsite_days: float
) -> float | None:
    """0–10 commute sub-score, or ``None`` when the commute is unknown.

    ``commute_minutes`` is the one-way door-to-door time from enrichment.
    ``onsite_days`` is what the LLM inferred from the posting (0 for fully
    remote, a hybrid default ~3 when unstated, up to 5 fully onsite).

    Returns ``None`` when ``commute_minutes`` is ``None`` (a ``needs_address``
    row, a routing failure, or an offer not yet enriched). ``None`` means
    *unknown*, not *zero*: the caller drops this criterion from the weighted
    total and flags it, rather than tanking or maxing the score (CLAUDE.md's
    unknown-commute rule). A fully-remote offer is enriched with
    ``commute_minutes = 0`` and correctly scores the maximum here.
    """
    if commute_minutes is None:
        return None

    weekly_total = commute_minutes * 2.0 * onsite_days
    base = _interpolate(weekly_total, _WEEKLY_ANCHORS)
    score = base - _oneway_penalty(commute_minutes)
    return max(0.0, min(10.0, score))

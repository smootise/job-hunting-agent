"""Jinja2 setup for the webapp: the ``templates/`` dir plus a few small filters.

The filters keep the templates declarative — a badge's CSS class, a rounded
minute count, a relative-weight percentage. Anything more than a one-liner would
belong in a route or the query layer, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# filter_status / score_status / review disposition → CSS modifier class
# (styled in app.css).
_BADGE_CLASSES = {
    "passed": "badge--ok",
    "scored": "badge--ok",
    "needs_review": "badge--warn",
    "rejected": "badge--bad",
    # review dispositions (webapp V2)
    "applied": "badge--ok",
    "to_review": "badge--warn",
    "not_interested": "badge--muted",
}


def _badge_class(status: str | None) -> str:
    """Map a status string to its badge CSS class (neutral if unknown/None)."""
    return _BADGE_CLASSES.get(status or "", "badge--muted")


def _round1(value: Any) -> str:
    """Render a number to one decimal, or an em dash when there's nothing."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any, of: float = 10.0) -> float:
    """A 0–100 percentage of ``value`` out of ``of`` (default a 0–10 score).

    Used to size the score bars/meters on the detail page. Clamped to [0, 100]
    so a stray out-of-range value can't overflow its track.
    """
    try:
        raw = float(value) / of * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return max(0.0, min(100.0, raw))


def _reltime(value: str | None) -> str:
    """Turn an ISO-8601 UTC timestamp into a compact 'x ago' string.

    Best-effort: an unparseable value is returned as-is rather than raising, so
    the dashboard never breaks on an odd stored timestamp.
    """
    if not value:
        return "never"
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def build_templates() -> Jinja2Templates:
    """Create the ``Jinja2Templates`` instance with our filters registered."""
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["badge_class"] = _badge_class
    templates.env.filters["round1"] = _round1
    templates.env.filters["pct"] = _pct
    templates.env.filters["reltime"] = _reltime
    return templates

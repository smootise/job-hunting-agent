"""Remembering the offer list's filters between visits.

The list view's whole state already lives in the URL query string (htmx's
``hx-push-url`` keeps the address bar in sync). That makes a filtered list
shareable and bookmarkable, but it also means *leaving* the page — clicking an
offer, then coming back via the nav — drops the filters and lands you on the
default view. This module persists the last-used query string so a bare
``/offers`` can restore it.

**Why a JSON file and not SQLite.** ``data/jobs.db`` is pipeline-owned: ingest,
filter and score write it, and ``offer_review`` was deliberately kept a separate
*table* to avoid entangling human state with pipeline state (see ``db.py``). This
is a third category again — per-browser UI preference, not data about offers —
so it gets its own small file rather than a table. It's disposable: losing it
costs one default-view page load.

**Failure is always soft.** Every read and write is wrapped: a corrupt file, a
read-only directory or a race between two writers degrades to "no saved view",
never an error page. A saved filter is a convenience; it must never be the reason
the app won't load.

**The stale-filter risk** is real and deliberately mitigated in the route, not
here: a restored view is announced in the UI and can be cleared with
``?reset=1``. A filter you forgot you set would otherwise hide offers and read
exactly like a bug — the silent-failure mode this project keeps designing away.
"""

from __future__ import annotations

import json
from pathlib import Path

# Query params that constitute "the view" and are therefore worth remembering.
# An allowlist, not "whatever was in the URL": it keeps one-shot params (the
# ``new=1`` preset, ``reset=1``) from being persisted and silently re-applied on
# every later visit, and bounds what a saved file can replay into the route.
REMEMBERED_PARAMS: frozenset[str] = frozenset(
    {
        "filter_status",
        "filter_status_set",
        "source",
        "score_status",
        "disposition",
        "posted_after",
        "seen_after",
        "hide_undated",
        "sort",
        "dir",
    }
)

# Cap on what we'll store/replay, so a hand-crafted URL can't grow the file
# without bound. Comfortably above any real selection (~13 checkboxes + dates).
_MAX_PAIRS = 60


def _state_path(project_root: Path) -> Path:
    return project_root / "data" / "view_state.json"


def save_view(project_root: Path, pairs: list[tuple[str, str]]) -> None:
    """Persist the current list view as ``(param, value)`` pairs.

    Pairs (not a dict) because the multi-select filters repeat a param — a dict
    would collapse ``source=wttj&source=linkedin_email`` to one value.
    """
    kept = [(k, v) for k, v in pairs if k in REMEMBERED_PARAMS][:_MAX_PAIRS]
    path = _state_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted write can't leave a truncated file
        # that the next read would have to discard.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"offers": kept}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a view we can't remember is not worth failing a page load over


def load_view(project_root: Path) -> list[tuple[str, str]]:
    """Return the saved ``(param, value)`` pairs, or ``[]`` if there's no usable one.

    Re-filters through ``REMEMBERED_PARAMS`` on the way out: the file is replayed
    into the route's parsing, so an edited or outdated file can only ever supply
    params the list view already understands (each of which the route still
    validates against its own allowlist).
    """
    try:
        raw = _state_path(project_root).read_text(encoding="utf-8")
        data = json.loads(raw)
        pairs = data["offers"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(pairs, list):
        return []
    out: list[tuple[str, str]] = []
    for item in pairs[:_MAX_PAIRS]:
        # Each entry round-trips through JSON as a 2-element list.
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
            and item[0] in REMEMBERED_PARAMS
        ):
            out.append((item[0], item[1]))
    return out


def clear_view(project_root: Path) -> None:
    """Forget the saved view (the ``?reset=1`` escape hatch)."""
    try:
        _state_path(project_root).unlink(missing_ok=True)
    except OSError:
        pass

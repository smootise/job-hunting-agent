"""Tests for the saved offer-list view (web/viewstate.py).

Covers the round trip, the repeated-param case the multi-select filters rely on,
the allowlist that keeps one-shot params (new=1) from being persisted, and the
fail-soft reads that must never break a page load.
"""

from __future__ import annotations

import json

from jobscout.web import viewstate


def test_save_and_load_round_trip(tmp_path):
    viewstate.save_view(tmp_path, [("sort", "posted_at"), ("dir", "asc")])
    assert viewstate.load_view(tmp_path) == [("sort", "posted_at"), ("dir", "asc")]


def test_repeated_params_are_preserved(tmp_path):
    """Multi-select filters repeat a param — a dict would collapse them."""
    pairs = [("source", "wttj"), ("source", "linkedin_email")]
    viewstate.save_view(tmp_path, pairs)
    assert viewstate.load_view(tmp_path) == pairs


def test_unlisted_params_are_not_saved(tmp_path):
    """One-shot params must not be persisted and silently replayed forever."""
    viewstate.save_view(tmp_path, [("new", "1"), ("reset", "1"), ("sort", "title")])
    assert viewstate.load_view(tmp_path) == [("sort", "title")]


def test_load_missing_file_is_empty(tmp_path):
    assert viewstate.load_view(tmp_path) == []


def test_load_corrupt_file_is_empty(tmp_path):
    """A damaged file degrades to "no saved view", never an exception."""
    path = tmp_path / "data" / "view_state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert viewstate.load_view(tmp_path) == []


def test_load_wrong_shape_is_empty(tmp_path):
    """Valid JSON of the wrong shape is discarded rather than crashing."""
    path = tmp_path / "data" / "view_state.json"
    path.parent.mkdir(parents=True)
    for payload in ('{"offers": "nope"}', '{"other": []}', "[]", '{"offers": [["a"]]}'):
        path.write_text(payload, encoding="utf-8")
        assert viewstate.load_view(tmp_path) == []


def test_load_filters_unknown_params_from_file(tmp_path):
    """An edited file can only replay params the list view understands."""
    path = tmp_path / "data" / "view_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"offers": [["sort", "title"], ["evil", "x"], ["new", "1"]]}),
        encoding="utf-8",
    )
    assert viewstate.load_view(tmp_path) == [("sort", "title")]


def test_clear_view(tmp_path):
    viewstate.save_view(tmp_path, [("sort", "title")])
    viewstate.clear_view(tmp_path)
    assert viewstate.load_view(tmp_path) == []


def test_clear_view_when_absent_is_noop(tmp_path):
    viewstate.clear_view(tmp_path)  # must not raise


def test_save_is_capped(tmp_path):
    """A hand-crafted URL can't grow the file without bound."""
    viewstate.save_view(tmp_path, [("source", f"s{i}") for i in range(500)])
    assert len(viewstate.load_view(tmp_path)) <= 60

"""Tests for the hand-rolled agent tool loop.

Fully offline: the model is a scripted ``generate`` stub returning canned turns,
tools are plain functions. Covers the loop's contract — the normal tool→final
cycle, tool-error fail-soft, the max_steps cap + forced final, the no-final
needs_review outcome, unparseable-action recovery, and that observations are
delimiter-fenced as untrusted data.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobscout.agents import loop


def _gen(scripted):
    """A ``generate`` stub that returns ``scripted[i]`` on the i-th call."""
    calls = {"i": 0}

    def generate(model, prompt, *, system=None, log_dir=None):
        i = calls["i"]
        calls["i"] += 1
        # Clamp so a forced-final call past the script reuses the last line.
        text = scripted[min(i, len(scripted) - 1)]
        return SimpleNamespace(response=text, prompt=prompt, log_path=log_dir)

    return generate


def _tools():
    def web_search(query):
        return [{"title": "ACME", "url": "https://acme.fr", "snippet": query}]

    return {"web_search": web_search}


def test_normal_cycle_tool_then_final():
    g = _gen([
        '{"tool": "web_search", "args": {"query": "acme"}}',
        '{"final": {"address": "12 rue X", "confidence": "high"}}',
    ])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=5)
    assert res.succeeded
    assert res.final["address"] == "12 rue X"
    assert res.stopped_reason == "final"
    assert len(res.steps) == 2
    assert res.steps[0].tool == "web_search"


def test_observation_is_fenced_as_untrusted():
    g = _gen([
        '{"tool": "web_search", "args": {"query": "x"}}',
        '{"final": {"ok": true}}',
    ])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=5)
    # The tool observation must be wrapped in the OBSERVATION delimiters.
    obs = res.steps[0].observation
    assert obs is not None
    # (the fence is applied in the transcript; the raw observation is the payload)
    assert "ACME" in obs


def test_unknown_tool_is_failsoft():
    g = _gen([
        '{"tool": "nope", "args": {}}',
        '{"final": {"done": true}}',
    ])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=5)
    assert res.succeeded
    assert "unknown tool" in res.steps[0].observation


def test_tool_exception_is_failsoft():
    def boom(**kwargs):
        raise RuntimeError("kaboom")

    g = _gen([
        '{"tool": "boom", "args": {}}',
        '{"final": {"done": true}}',
    ])
    res = loop.run_agent("sys", "task", {"boom": boom}, generate=g, max_steps=5)
    assert res.succeeded
    assert "failed" in res.steps[0].observation


def test_bad_args_is_failsoft():
    g = _gen([
        '{"tool": "web_search", "args": {"wrong": "kw"}}',
        '{"final": {"done": true}}',
    ])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=5)
    assert res.succeeded
    assert "bad arguments" in res.steps[0].observation


def test_max_steps_forces_final():
    g = _gen([
        '{"tool": "web_search", "args": {"query": "a"}}',
        '{"tool": "web_search", "args": {"query": "b"}}',
        '{"final": {"forced": true}}',  # the forced-final call
    ])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=2)
    assert res.succeeded
    assert res.stopped_reason == "forced_final"
    assert res.final["forced"] is True


def test_never_final_yields_needs_review():
    g = _gen(["garbage", "still garbage", "no json here"])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=2)
    assert not res.succeeded
    assert res.final is None
    assert res.stopped_reason == "no_final"


def test_unparseable_action_recovers_next_turn():
    g = _gen([
        "I think I should search... (no JSON)",
        '{"final": {"recovered": true}}',
    ])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=5)
    assert res.succeeded
    assert res.final["recovered"] is True
    assert res.steps[0].error is not None  # first turn flagged unparseable


def test_fenced_json_is_parsed():
    g = _gen(['```json\n{"final": {"ok": 1}}\n```'])
    res = loop.run_agent("sys", "task", _tools(), generate=g, max_steps=3)
    assert res.succeeded and res.final["ok"] == 1


@pytest.mark.parametrize("bad", ['{"neither": 1}', "[]", "not json", ""])
def test_parse_action_rejects_non_actions(bad):
    assert loop._parse_action(bad) is None

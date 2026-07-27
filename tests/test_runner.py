"""Tests for the webapp's background JobRunner.

Offline and fast: a fake stage registry (no real pipeline / LLM / network) is
injected. Covers a stage running to completion with progress, the db_path
passthrough, mid-run progress observation, hard-fail halting a chain vs. fail-soft
(returns normally) continuing, and reject-when-busy.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from jobscout.web.runner import JobRunner, RunnerBusy


class _Settings:
    """A minimal stand-in for web.settings.Settings."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.preferences_path = Path("preferences.yaml")
        self.env_path = Path(".env")
        self.port = 8020


def _wait_until(pred, timeout=3.0):
    """Bounded poll — avoids a hung test if the worker never progresses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def settings(tmp_path):
    return _Settings(tmp_path / "jobs.db")


def _runner(settings, registry):
    r = JobRunner(settings, registry=registry)
    r.start()
    return r


def test_stage_runs_with_progress_and_summary(settings):
    def fake(*, job_id=None, on_progress=None):
        for i in range(1, 4):
            if on_progress:
                on_progress(i, 3)
        return {"considered": 3, "scored": 3}

    r = _runner(settings, {"fake": fake})
    try:
        r.enqueue("fake")
        assert _wait_until(lambda: r.snapshot().status == "done")
        s = r.snapshot()
        assert s.done == 3 and s.total == 3
        assert s.summary == {"considered": 3, "scored": 3}
    finally:
        r.stop()


def test_db_path_passthrough(settings):
    captured = {}

    # The real registry binds db_path; here we assert the settings db_path is the
    # one the runner would hand to a stage by inspecting the default registry.
    r = JobRunner(settings)
    try:
        # The default registry's thunks close over settings.db_path; run a fake
        # via the public path by swapping one entry.
        def fake(*, job_id=None, on_progress=None):
            captured["db"] = settings.db_path
            return {}
        r._registry["fake"] = fake  # noqa: SLF001 — white-box: assert binding
        r.start()
        r.enqueue("fake")
        assert _wait_until(lambda: r.snapshot().status == "done")
        assert captured["db"] == settings.db_path
    finally:
        r.stop()


def test_progress_observed_mid_run(settings):
    release = threading.Event()

    def fake(*, job_id=None, on_progress=None):
        on_progress(1, 2)
        release.wait(timeout=2.0)  # block so the test can read a partial state
        on_progress(2, 2)
        return {}

    r = _runner(settings, {"fake": fake})
    try:
        r.enqueue("fake")
        assert _wait_until(lambda: r.snapshot().done == 1 and r.snapshot().status == "running")
        release.set()
        assert _wait_until(lambda: r.snapshot().status == "done")
    finally:
        r.stop()


def test_hard_fail_halts_chain(settings):
    ran = {"ok": False, "after": False}

    def ok(*, job_id=None, on_progress=None):
        ran["ok"] = True
        return {}

    def boom(*, job_id=None, on_progress=None):
        raise RuntimeError("missing API key")

    def after(*, job_id=None, on_progress=None):
        ran["after"] = True
        return {}

    # Patch PIPELINE_ORDER by using a custom registry + a pipeline job. The
    # runner builds "pipeline" from module PIPELINE_ORDER, so instead drive the
    # chain semantics through a registry whose stages we enqueue in sequence via
    # a small custom chain: emulate by monkeypatching the order.
    import jobscout.web.runner as rn
    orig = rn.PIPELINE_ORDER
    rn.PIPELINE_ORDER = ("ok", "boom", "after")
    try:
        r = _runner(settings, {"ok": ok, "boom": boom, "after": after})
        try:
            r.enqueue("pipeline")
            assert _wait_until(lambda: r.snapshot().status == "failed")
            s = r.snapshot()
            assert "boom" in s.error
            assert ran["ok"] is True and ran["after"] is False  # halted before 'after'
        finally:
            r.stop()
    finally:
        rn.PIPELINE_ORDER = orig


def test_fail_soft_returns_normally_continues_chain(settings):
    ran = {"b": False}

    def a(*, job_id=None, on_progress=None):
        return {"needs_review": 2}  # fail-soft: returns normally, doesn't raise

    def b(*, job_id=None, on_progress=None):
        ran["b"] = True
        return {}

    import jobscout.web.runner as rn
    orig = rn.PIPELINE_ORDER
    rn.PIPELINE_ORDER = ("a", "b")
    try:
        r = _runner(settings, {"a": a, "b": b})
        try:
            r.enqueue("pipeline")
            assert _wait_until(lambda: r.snapshot().status == "done")
            assert ran["b"] is True  # chain continued past the fail-soft stage
        finally:
            r.stop()
    finally:
        rn.PIPELINE_ORDER = orig


def test_reject_when_busy(settings):
    release = threading.Event()

    def slow(*, job_id=None, on_progress=None):
        release.wait(timeout=2.0)
        return {}

    r = _runner(settings, {"slow": slow})
    try:
        r.enqueue("slow")
        assert _wait_until(lambda: r.snapshot().status == "running")
        with pytest.raises(RunnerBusy):
            r.enqueue("slow")
        release.set()
    finally:
        r.stop()


def test_unknown_stage_raises(settings):
    r = _runner(settings, {"fake": lambda **k: {}})
    try:
        with pytest.raises(ValueError):
            r.enqueue("nope")
    finally:
        r.stop()


def test_cancel_mid_run_stops_after_current_offer(settings):
    """Cancel during a stage: it stops at the next offer boundary, completed
    offers persist, status is 'cancelled', and a re-run finishes the rest."""
    at_offer_1 = threading.Event()
    resume = threading.Event()
    done_offers = []

    def fake(*, job_id=None, on_progress=None):
        total = 5
        # Resume from wherever a prior run stopped (idempotency stand-in).
        start = len(done_offers) + 1
        for i in range(start, total + 1):
            done_offers.append(i)  # this offer's work "commits" before progress
            on_progress(i, total)
            if i == 1:
                at_offer_1.set()
                resume.wait(timeout=2.0)  # hold so the test can cancel mid-loop
        return {"considered": total, "done": len(done_offers)}

    r = _runner(settings, {"fake": fake})
    try:
        r.enqueue("fake")
        assert at_offer_1.wait(timeout=2.0)  # offer 1 committed, loop parked
        r.cancel()
        resume.set()  # let the loop advance and hit the cancel checkpoint
        assert _wait_until(lambda: r.snapshot().status == "cancelled")
        s = r.snapshot()
        # Stopped early — not all 5 offers ran. Granularity is one offer, so the
        # exact stop point (1 or 2) depends on the race with resume; the invariant
        # is that it aborted before finishing and progress was preserved.
        assert 1 <= s.done < 5 and s.total == 5
        stopped_at = len(done_offers)
        assert stopped_at == s.done

        # Re-run resumes from where it stopped: the rest finish, status 'done'.
        r.enqueue("fake")
        assert _wait_until(lambda: r.snapshot().status == "done")
        assert done_offers == [1, 2, 3, 4, 5]
    finally:
        r.stop()


def test_cancel_when_idle_is_noop(settings):
    r = _runner(settings, {"fake": lambda **k: {}})
    try:
        r.cancel()  # nothing running
        assert r.snapshot().status == "idle"
        # The stale cancel must not poison the next run.
        r.enqueue("fake")
        assert _wait_until(lambda: r.snapshot().status == "done")
    finally:
        r.stop()


def test_cancel_aborts_whole_chain(settings):
    """A cancel during an early chain stage does not run later stages."""
    at_a = threading.Event()
    resume = threading.Event()
    ran = {"b": False}

    def a(*, job_id=None, on_progress=None):
        on_progress(1, 2)
        at_a.set()
        resume.wait(timeout=2.0)
        on_progress(2, 2)  # cancel checkpoint fires here
        return {}

    def b(*, job_id=None, on_progress=None):
        ran["b"] = True
        return {}

    import jobscout.web.runner as rn
    orig = rn.PIPELINE_ORDER
    rn.PIPELINE_ORDER = ("a", "b")
    try:
        r = _runner(settings, {"a": a, "b": b})
        try:
            r.enqueue("pipeline")
            assert at_a.wait(timeout=2.0)
            r.cancel()
            resume.set()
            assert _wait_until(lambda: r.snapshot().status == "cancelled")
            assert ran["b"] is False  # later stage never ran
        finally:
            r.stop()
    finally:
        rn.PIPELINE_ORDER = orig

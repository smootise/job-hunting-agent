"""A single-worker background runner for triggering pipeline stages from the UI.

The webapp is otherwise read-only; this is the one place it *does* work — but it
runs the **same** ``pipeline.run_*`` functions the CLI runs, in a background
thread, so a click on "Run filter" is exactly ``jobscout filter`` with a progress
callback wired in. No new external actions, no new capabilities.

Design (and why):

* **One worker thread, strictly single-flight.** The ``run_*`` stages each open
  their own SQLite connection and commit as they go; SQLite (WAL) permits only
  one *writer* at a time, so running two stages at once risks ``SQLITE_BUSY``.
  A single worker + **reject-when-busy** enqueue guarantees at most one writer
  ever. Read requests are unaffected (WAL lets readers and the one writer coexist).

* **In-process state is the source of truth for "is a run active".** The ``runs``
  ledger's ``finished_at IS NULL`` is *not* reliable for this — a hard-killed
  process leaves a phantom "running" row forever. So the UI reads
  ``JobRunner.snapshot()`` (live thread state), and the ledger stays history.

* **Explicit ``db_path``.** Every stage is called with
  ``db_path=settings.db_path`` (the project-root-anchored path the web layer
  resolved), never the ``run_*`` CWD-relative default — otherwise a server
  launched from another directory would drive a different DB than the routes read.

* **The pipeline chain resumes for free.** Because every stage is idempotent by
  DB state (each worklist is gated on ``*_at IS NULL`` etc.), a chain that halts
  on a stage error can be re-run: completed stages find nothing to do (fast
  no-op) and the failed stage resumes on its remaining offers. No bookkeeping.

Single-worker caveat: this in-process design assumes uvicorn runs one worker
(the ``serve`` reality). Under ``--reload`` or ``workers>1`` the runner and the
status endpoint could live in different processes — don't enable those for the
run buttons.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone

from jobscout.pipeline import (
    enrich_commute,
    enrich_linkedin,
    filter_stage,
    ingest,
    research_address,
    research_company,
    score_stage,
)

logger = logging.getLogger("jobscout.web.runner")

# The chain the "Run whole pipeline" button runs, in dependency order (matches
# docs/README): research agents BEFORE enrich-commute (the sole router), scoring
# last. Halts on the first stage that raises; re-running resumes (idempotency).
PIPELINE_ORDER = (
    "ingest",
    "filter",
    "enrich-linkedin",
    "research-address",
    "research-company",
    "enrich-commute",
    "score",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    """The current/last job's live state, read by the status endpoint.

    ``status`` transitions idle → running → (done | failed). ``done``/``total``
    drive the progress bar (``total==0`` means "not counted yet / nothing to do").
    ``summary`` is the finished stage's summary as a plain dict; ``error`` is set
    only on a hard failure (a stage that raised).
    """

    stage: str | None = None
    status: str = "idle"  # 'idle' | 'running' | 'done' | 'failed'
    done: int = 0
    total: int = 0
    summary: dict | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class _Job:
    """A queued unit of work: a single stage or the pipeline chain."""

    name: str            # the stage key, or "pipeline"
    job_id: int | None = None  # for the parametrized "rescore" stage
    stages: tuple[str, ...] = field(default_factory=tuple)  # for "pipeline"


class RunnerBusy(Exception):
    """Raised by ``enqueue`` when a job is already running/queued (single-flight)."""


class JobRunner:
    """Owns the worker thread, the queue, and the shared job state.

    Constructed with ``settings`` (for ``db_path``/``preferences_path``/
    ``env_path``). A test may inject a ``registry`` mapping stage-key → callable
    to avoid running real pipeline stages.
    """

    def __init__(self, settings, *, registry: dict | None = None) -> None:
        self._settings = settings
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._lock = threading.Lock()
        self._state = JobState()
        self._thread: threading.Thread | None = None
        self._registry = registry if registry is not None else self._default_registry()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the single daemon worker thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker, name="jobscout-runner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown and join the worker (bounded wait)."""
        self._queue.put(None)  # sentinel
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # -- public API used by routes ----------------------------------------

    def enqueue(self, name: str, *, job_id: int | None = None) -> None:
        """Queue a stage (or "pipeline") to run; reject if one is already active.

        Single-flight: raises ``RunnerBusy`` if a job is running or already
        queued, ``ValueError`` if ``name`` isn't a known stage. Returns
        immediately; the worker does the work.
        """
        if name == "pipeline":
            job = _Job(name="pipeline", stages=PIPELINE_ORDER)
        else:
            if name not in self._registry:
                raise ValueError(f"unknown stage: {name!r}")
            job = _Job(name=name, job_id=job_id)

        with self._lock:
            if self._state.status == "running" or not self._queue.empty():
                raise RunnerBusy("a run is already in progress")
        self._queue.put(job)

    def snapshot(self) -> JobState:
        """A copy of the current state, safe to read outside the lock."""
        with self._lock:
            return replace(self._state)

    # -- the worker --------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # shutdown sentinel
                return
            try:
                self._run_job(job)
            except Exception:  # noqa: BLE001 — never let the worker thread die.
                logger.exception("runner: unexpected error running %s", job.name)

    def _run_job(self, job: _Job) -> None:
        """Run a single stage or the pipeline chain, updating state as it goes."""
        stages = job.stages or (job.name,)
        with self._lock:
            self._state = JobState(
                stage=job.name, status="running", started_at=_utcnow()
            )

        last_summary: dict | None = None
        for stage in stages:
            # Reflect the current sub-stage (for the pipeline chain) and reset the
            # per-stage progress counters.
            with self._lock:
                self._state.stage = stage
                self._state.done = 0
                self._state.total = 0
            try:
                last_summary = self._run_stage(stage, job_id=job.job_id)
            except Exception as exc:  # noqa: BLE001 — a HARD stage failure.
                # A stage that *raises* (missing key, unreachable service) halts
                # the chain. Per-row fail-soft never reaches here (those stages
                # return normally with a needs_review/failed count).
                logger.warning("runner: stage %s failed: %s", stage, exc)
                with self._lock:
                    self._state.status = "failed"
                    self._state.error = f"{stage}: {type(exc).__name__}: {exc}"
                    self._state.finished_at = _utcnow()
                return

        with self._lock:
            self._state.status = "done"
            self._state.summary = last_summary
            self._state.finished_at = _utcnow()

    def _run_stage(self, stage: str, *, job_id: int | None) -> dict:
        """Invoke one stage's callable with the progress callback; return summary."""
        thunk = self._registry[stage]

        def progress(done: int, total: int) -> None:
            with self._lock:
                self._state.done = done
                self._state.total = total

        summary = thunk(job_id=job_id, on_progress=progress)
        # Summaries are dataclasses; normalize to a plain dict for the template.
        try:
            return asdict(summary)
        except TypeError:
            return dict(summary) if isinstance(summary, dict) else {"result": str(summary)}

    # -- the real stage registry ------------------------------------------

    def _default_registry(self) -> dict:
        """Map each stage key to a thunk calling the real ``run_*`` function.

        Every thunk takes ``(*, job_id=None, on_progress=None)`` (a uniform shape
        the worker calls) and binds the runner-supplied paths. ``db_path`` is
        always the resolved settings path — never the CWD default.
        """
        s = self._settings
        db_path = s.db_path
        prefs = s.preferences_path
        env = s.env_path

        def _ingest(*, job_id=None, on_progress=None):
            return ingest.run_ingest(db_path=db_path, on_progress=on_progress)

        def _filter(*, job_id=None, on_progress=None):
            return filter_stage.run_filter(
                prefs_path=prefs, db_path=db_path, on_progress=on_progress
            )

        def _enrich_linkedin(*, job_id=None, on_progress=None):
            return enrich_linkedin.run_enrich_linkedin(
                prefs_path=prefs, db_path=db_path, on_progress=on_progress
            )

        def _enrich_commute(*, job_id=None, on_progress=None):
            return enrich_commute.run_enrich_commute(
                prefs_path=prefs, env_path=env, db_path=db_path, on_progress=on_progress
            )

        def _research_address(*, job_id=None, on_progress=None):
            return research_address.run_research_address(
                prefs_path=prefs, env_path=env, db_path=db_path, on_progress=on_progress
            )

        def _research_company(*, job_id=None, on_progress=None):
            return research_company.run_research_company(
                prefs_path=prefs, env_path=env, db_path=db_path, on_progress=on_progress
            )

        def _score(*, job_id=None, on_progress=None):
            return score_stage.run_score(
                prefs_path=prefs, db_path=db_path, on_progress=on_progress
            )

        def _score_commute_only(*, job_id=None, on_progress=None):
            return score_stage.run_score(
                prefs_path=prefs, db_path=db_path, commute_only=True,
                on_progress=on_progress,
            )

        def _rescore(*, job_id=None, on_progress=None):
            # Targeted re-score of one offer (score --ids). job_id is required.
            return score_stage.run_score(
                prefs_path=prefs, db_path=db_path, ids=[job_id],
                on_progress=on_progress,
            )

        return {
            "ingest": _ingest,
            "filter": _filter,
            "enrich-linkedin": _enrich_linkedin,
            "enrich-commute": _enrich_commute,
            "research-address": _research_address,
            "research-company": _research_company,
            "score": _score,
            "score-commute-only": _score_commute_only,
            "rescore": _rescore,
        }

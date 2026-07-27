# Plan: cooperative run-cancel from the webapp

## Goal
Let the owner cleanly stop an in-progress run from the webapp (to free the GPU)
without killing the `jobscout serve` process. The next run resumes from where it
stopped, exactly like a `Ctrl+C` does today — but the server stays up.

## Decisions (owner-confirmed)
- **Cancel-only.** No freeze-style pause/resume. Because every stage is idempotent
  by DB state, "Stop now, re-press later" already *is* pause with zero held state.
- **Whole-chain cancel.** Stop aborts after the current offer and does NOT continue
  to later stages. Fastest GPU release; re-press resumes the chain.

## Mechanism (one-offer granularity)
Every stage's loop already calls `on_progress(done, total)` once per offer, *after*
that offer is committed. The runner's `progress` closure becomes the cancel
checkpoint: it checks a `threading.Event` and raises `RunCancelled` when set. The
exception unwinds through each stage's `finally` (commit + `record_run_finish`), so:
- the in-flight offer's work is never wasted (it already committed), and
- the next run resumes for free (idempotency — completed offers are gated out).

Cancel granularity = one offer (seconds on research/score stages). A hung LLM call
still needs `Ctrl+C` — documented limitation, acceptable given idempotency.

## Changes
1. **`web/runner.py`**
   - `class RunCancelled(Exception)`.
   - `JobRunner`: add `self._cancel = threading.Event()`.
   - `enqueue(...)`: `self._cancel.clear()` when accepting a job (fresh run).
   - `cancel()`: `self._cancel.set()` (no-op if idle).
   - `progress` closure in `_run_stage`: after updating counters, `if self._cancel.is_set(): raise RunCancelled`.
   - `_run_job`: catch `RunCancelled` in the stage loop → set status `cancelled`,
     record `finished_at`, and `break`/`return` (do not run later chain stages).
2. **`web/routes.py`**
   - `POST /runs/cancel` behind `require_local_origin`; calls `runner.cancel()`;
     returns the `runs/_status.html` fragment.
3. **`templates/runs/_status.html`**
   - **Stop** button (`hx-post="/runs/cancel"`) visible only while `status == 'running'`.
   - New `cancelled` branch: badge + "Stopped after N/M — re-run to resume."
4. **Tests** (`tests/`)
   - Fake multi-offer stage via injected `registry`; cancel mid-loop → status
     `cancelled`, completed offers persisted, re-run finishes the remainder.
5. **Docs**: CLAUDE.md webapp bullet + `docs/webapp.md` note the cancel seam.

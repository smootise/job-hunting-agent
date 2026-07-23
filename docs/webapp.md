# Webapp (as-built)

A local web UI over `data/jobs.db`: a stats dashboard, a sortable/filterable
ranked offer list, and a per-offer detail page. It's how the owner browses what
the pipeline produced instead of reading SQLite by hand (the `digest` command was
never built — this is its replacement), and now also **tracks the owner's own job
hunt** and **triggers pipeline stages from the UI**.

- **V1 (read-only):** dashboard / list / detail.
- **V2 (writes):** *application tracking* (mark each offer to-review / applied /
  not-interested + notes) and *run-CLI buttons* (trigger any pipeline stage, or
  the whole pipeline, via a background runner with a progress bar).

CLI: `jobscout serve [--host H] [--port 8020] [--reload]`.

Code: `web/` (FastAPI app) + read/stats/write layers in `storage/`:
`storage/queries.py` (display reads), `storage/stats.py` (dashboard + review
counts), and the `offer_review` table + `upsert_review` in `storage/db.py`.

Stack: **FastAPI + Jinja2 + HTMX**, server-rendered, no build step. htmx is
**vendored locally** (`web/static/htmx.min.js`) — a strict "fully local, no CDN"
choice, same as the no-cloud-LLM rule.

## Golden-rule posture (why this is safe)

The read routes are `GET` and change nothing. The V2 **write** routes (review +
run triggers) never send email, never log in anywhere, and the run triggers
execute **only the existing `pipeline.run_*` code** the CLI already runs — no new
external capability is added, the runner just calls it from a background thread.
All write routes sit behind a **same-origin guard** (`web/security.py`): a
loopback single-user tool with no ambient credentials, so a same-origin check on
`Origin`/`Referer` is proportionate; a CSRF-token scheme would be ceremony. The
key read-only invariants still hold on the read routes:
the only I/O is reading the local DB. So "the agent never acts externally" holds
trivially. Two specifics worth stating:

- **Office coordinates are shown; home coordinates are not.** The `jobs` table
  stores the *office* `lat`/`lon` (public), which the detail page may display and
  a future map may pin. The owner's home location is never in the `jobs` table,
  never in a template — commute is shown as *minutes* only, exactly as
  everywhere else in the project.
- **JSON columns are decoded defensively.** `score_json` / `company_brief` /
  `commute_strategies` / `filter_reasons` are TEXT and can be NULL or (in at
  least one live row) contain mojibake; `queries.parse_json_column` swallows both
  so one bad row can't 500 a list render.

## The read layers (the foundation)

Before this, every `db.select_*` was a *pipeline worklist* query (gated on a
`*_at IS NULL` predicate). There was no "list all offers" or "get offer by id",
and no counts. Two small modules fill that gap:

### `storage/queries.py` — display reads

- `get_job(conn, id) -> Row | None`.
- `list_jobs(conn, *, filter_status, source, score_status, disposition, order_by,
  descending, limit, offset, criteria_names) -> list[Row]`. Sorting is
  **allowlist-guarded**: a plain column must be in `ALLOWED_SORT_COLUMNS`; a
  `"criteria:<name>"` sort is validated against the rubric criteria (from
  `preferences.yaml`) before its JSON path is built server-side — a raw sort
  string is **never** interpolated into SQL. Sorting happens in SQLite via
  `json_extract` in the `ORDER BY`, with a NULLS-LAST idiom
  (`<expr> IS NULL, <expr> DESC`) so unscored rows sink to the bottom.
  `weekly_commute_fit` lives at `$.weekly_commute_fit` (top-level), not inside
  `$.criteria_scores`.
- Both `get_job`/`list_jobs` **LEFT JOIN `offer_review`** (V2), so jobs columns
  are qualified `jobs.` and review columns `r.` (qualification is harmless
  without the join). The `disposition` filter accepts a real value (equality) or
  the `queries.UNREVIEWED` sentinel (`r.disposition IS NULL` — no review row).
- `parse_json_column` / `hydrate_job` — decode the TEXT JSON columns once so
  templates stay dumb (no `json.loads` in Jinja). `hydrate_job` also nests the
  joined review columns as `data["review"]` (`job.review.disposition`).

### `storage/stats.py` — dashboard aggregates

`dashboard_stats(conn)` (funnel + per-stage pending counts), `last_run(conn)` /
`recent_runs(conn)` (parsed `runs` ledger), and (V2) `review_stats(conn) ->
ReviewStats` (counts per disposition + `unreviewed` = jobs with no review row).
The "pending" counts **mirror the `db.select_jobs_to_*` predicates exactly** — a
test (`tests/test_stats.py`) asserts each count equals `len(select_jobs_to_*)` on
the same DB, so the duplicated `WHERE` clauses can't silently drift. Review
counts have no worklist counterpart, so they're asserted directly instead.

### `storage/db.py` — the `offer_review` write layer (V2)

A **separate** `offer_review(job_id PK → jobs.id, disposition, applied_at, notes,
updated_at)` table — deliberately not columns on `jobs` (that table is
pipeline-owned; this is human-owned). Created idempotently by `_SCHEMA` (like
`runs`), so an existing DB gets it on next `connect`. `upsert_review(conn,
job_id, *, disposition, notes)` is an `INSERT ... ON CONFLICT(job_id) DO UPDATE`
(caller commits, like `record_score`); the `disposition` domain is validated in
Python (`REVIEW_DISPOSITIONS`), and **`applied_at` is preserved** — it's stamped
once when an offer is first marked *applied* and kept across later notes edits, so
editing a note never moves the applied date. `connect` also sets
`PRAGMA busy_timeout=5000` (a review POST and a running stage are two writers;
WAL + busy_timeout keeps them from colliding).

## The app (`web/`)

- `settings.py` — resolves an explicit **project root** (walks up to
  `pyproject.toml`) so the server finds `data/jobs.db` + `preferences.yaml` +
  `.env` regardless of the launch directory (the CLI's defaults are CWD-relative).
  Env overrides: `JOBSCOUT_DB_PATH`, `JOBSCOUT_PREFERENCES_PATH`,
  `JOBSCOUT_ENV_PATH`, `JOBSCOUT_HOST`, `JOBSCOUT_PORT`. **The port must reach
  `Settings`** because the same-origin guard checks against it — and `serve` runs
  the *module-level* `app` (built with no args), so the CLI exports
  `JOBSCOUT_HOST`/`JOBSCOUT_PORT` before uvicorn imports the app rather than
  passing them as arguments. (Missing this was a real bug: writes 403'd on any
  non-default port.)
- `app.py` — `create_app(settings)` factory (+ module-level `app` for the uvicorn
  import string). Caches the rubric criteria on `app.state`. A **`lifespan`**
  handler (V2) starts/stops the background `JobRunner` and stores it on
  `app.state.runner`; on startup it logs (never rewrites) any interrupted
  `runs`-ledger rows.
- `dependencies.py` — a **per-request** DB connection (WAL allows concurrent
  reads; sqlite3 connections aren't thread-safe to share).
- `runner.py` (V2) — the background job runner (see below). `security.py` (V2) —
  the same-origin guard. `routes.py`, `templating.py`, `templates/`, `static/`.

### Routes

Read routes (`GET`):

| Path | Renders |
|---|---|
| `/` | dashboard: agent-pipeline funnel + pending work + last run + **My review** + **Run panel** |
| `/offers` | ranked offer list (filter form + table) |
| `/offers/table` | just the `<table>` — the HTMX target for sort/filter swaps |
| `/offers/{id}` | offer detail (404 → friendly page) |
| `/runs/status` | the self-polling run-status fragment |
| `/healthz` | `{"ok": true}` liveness |

Write routes (`POST`, all behind `require_local_origin`) — V2:

| Path | Does |
|---|---|
| `/offers/{id}/review` | `upsert_review` (disposition + notes) → swaps back the review control |
| `/runs/{stage}` | enqueue a stage (or `pipeline`) on the whole worklist → status fragment; 404 unknown; busy is harmless |
| `/offers/{id}/runs/{stage}` | enqueue a stage on **one offer** (`<stage> --ids=<id>`, forced past the done-gate) → status fragment; 404 if the stage isn't per-offer (e.g. `ingest`) |

`/offers` and `/offers/table` share one `_table.html` partial, so first paint
and every HTMX sort/filter swap render identically. A bad `sort` param is a
`400` (not a silent fallback), so the UI can't drift into an un-sorted state
unnoticed.

### The background runner (`web/runner.py`)

A **single-worker, in-process** runner: a `queue.Queue`, one daemon thread, and a
lock-guarded `JobState` (stage / status / done / total / summary / error), owned
by the app via the lifespan handler. It runs the **same `pipeline.run_*`
functions** the CLI runs — a click on "Run filter" is `jobscout filter` with a
progress callback wired in.

- **Strictly single-flight (reject-when-busy).** The stages each open their own
  SQLite connection and commit as they go; WAL permits only one *writer*, so a
  single worker + refusing a second enqueue while one is active guarantees at
  most one writer ever. Reads are unaffected (WAL readers coexist with the writer).
- **Explicit `db_path`/`env_path`.** Every stage is called with the resolved
  settings paths, never the `run_*` CWD-relative defaults, so the runner and the
  request handlers always touch the same DB.
- **Progress bar.** Each `run_*` gained an optional `on_progress(done, total)`
  callback (`pipeline.ProgressFn`) — one additive line per per-row loop, default
  `None` so the CLI path is unchanged. The runner passes one that updates
  `JobState`; the status fragment renders a `done/total` bar. (For ingest the
  unit is a *source*, not an offer — total-fetched isn't knowable upfront.)
- **The pipeline chain resumes for free.** "Run whole pipeline" runs the stages
  in dependency order (research agents *before* enrich-commute, the sole router;
  scoring last) and **halts on the first stage that _raises_** (a hard
  precondition failure — missing API key / SearXNG). Per-row fail-soft (a row
  marked needs_review) returns normally and does *not* halt. Because every stage
  is idempotent by DB state, **re-pressing the button resumes**: completed stages
  find an empty worklist (fast no-op) and the failed stage picks up its remaining
  offers. No resume bookkeeping.
- **Crash phantoms.** The `runs` ledger's `finished_at IS NULL` is *not* a
  reliable "running now" signal (a hard-killed process leaves it forever). The UI
  reads the in-process `runner.snapshot()` for live status; the ledger stays
  history.
- **Single-worker caveat.** This assumes uvicorn runs one worker (the `serve`
  reality). Under `--reload` or `workers>1` the runner and status endpoint could
  live in different processes — don't enable those for the run buttons.

### Pages

- **Dashboard** — the **Agent pipeline** section (total; by `filter_status`;
  scored; per-stage backlog; last-run card); the **My review** section (V2, live:
  unreviewed / to-review / applied / not-interested counts from `review_stats`);
  and the **Run panel** (V2: a button per stage + "Run whole pipeline" + a
  commute-only button, each `hx-post`-ing to `/runs/*` and swapping the
  self-polling status fragment).
- **Offer list** — score / title / company / source / status badges / **review
  badge** / commute + remote chip / red-flag count. Sortable by the overall score
  **or any single rubric criterion** via the sort dropdown, plus
  status/source/**disposition** filters — all HTMX partial swaps.
- **Offer detail** — a **My review** card (V2: the disposition + notes control
  that POSTs and swaps itself back) and a **"Run a step on this offer"** panel
  (V2: re-run any applicable stage on just this offer — filter / enrich-linkedin /
  enrich-commute / research-address / research-company / (re)score). Visibility:
  enrich-linkedin only on `linkedin_email` offers; the pipeline stages are hidden
  on rejected offers (they'd no-op — the eligibility gate is a real safety rail,
  not idempotency); re-filter always shows (it can un-reject after a prefs
  change). The expensive LLM/network re-runs carry an `hx-confirm`. Then four
  blocks: summary + verdict; full score breakdown (each criterion's 0–10 with its
  relative weight — weights sum to 73); commute detail (best + three strategies
  with per-leg breakdown + resolved address/source); company research + raw
  posting + metadata with an outbound "View original posting" link. A **hidden
  office-map slot** carries the office `lat`/`lon` for a future map.

Per-offer targeting reuses the `score --ids` mechanism, now generalized: each
worklist selector (`select_unfiltered_jobs`, `select_jobs_to_enrich`,
`select_jobs_to_research_*`, `select_jobs_missing_description`) takes an `ids`
param that (a) restricts to those ids, (b) **drops the "already done" gate** so a
deliberate re-run works, but (c) **keeps the real eligibility gate** (a rejected
offer selects nothing for the pipeline stages — a safe no-op). The runner's
stage thunks are dual-purpose: `job_id=None` runs the whole worklist (dashboard),
a `job_id` passes `ids=[job_id]` (detail page).

## Shutdown

`jobscout serve` (no `--reload`) drives a `uvicorn.Server` with a custom
`SIGINT`/`SIGTERM`/`SIGBREAK` handler that flips `should_exit`, plus a short
`timeout_graceful_shutdown` — so a single Ctrl+C tears the server down cleanly on
Windows (whose default uvicorn handling can miss an idle-loop Ctrl+C and orphan
the worker). `--reload` uses uvicorn's own supervisor, whose child handles Ctrl+C.

## Still deferred (not built)

- **Company-location map:** the `#office-map` slot stays hidden; wire it with a
  locally-vendored map + offline tiles (office coords only) in a later pass.
- **Multi-stage ATS** (Applied → Phone → Interview → Offer): V2 tracking is the
  simple three-state set only.
- **Live log streaming** and **run cancellation:** the status fragment polls for
  a progress bar; there is no per-line log and no cancel hook in the `run_*` loops.
- **Dedicated LLM posting summary:** the summary reuses existing
  scorer/company-brief output (no extra model call).

## Tests

`tests/test_queries.py` (read layer + the sort allowlist guard),
`tests/test_stats.py` (counts + the worklist-parity invariant),
`tests/test_review.py` (V2: `offer_review` migration, `upsert_review` incl. the
preserve-`applied_at` rule, `review_stats`, the `disposition` filter + join),
`tests/test_runner.py` (V2: the runner with a fake stage registry — progress,
`db_path` passthrough, hard-fail-halts vs. fail-soft-continues, reject-when-busy),
`tests/test_web.py` (route smoke tests, read + write, via FastAPI `TestClient`
against a fixture DB + `preferences.example.yaml`; write-route tests use the
context-manager `TestClient` so the runner lifespan starts). All offline.

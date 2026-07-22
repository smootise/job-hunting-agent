# Webapp (as-built)

A local, **read-only** web UI over `data/jobs.db`: a stats dashboard, a
sortable/filterable ranked offer list, and a per-offer detail page. It's how the
owner browses what the pipeline produced instead of reading SQLite by hand (the
`digest` command was never built — this is its replacement).

CLI: `jobscout serve [--host H] [--port 8020] [--reload]`.

Code: `web/` (FastAPI app) + two new read layers in `storage/`:
`storage/queries.py` (display reads) and `storage/stats.py` (dashboard counts).

Stack: **FastAPI + Jinja2 + HTMX**, server-rendered, no build step. htmx is
**vendored locally** (`web/static/htmx.min.js`) — a strict "fully local, no CDN"
choice, same as the no-cloud-LLM rule.

## Golden-rule posture (why this is safe)

V1 is read-only by construction — there is **no route that writes, triggers a
pipeline stage, sends email, or makes an external call.** Every route is `GET`;
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
- `list_jobs(conn, *, filter_status, source, score_status, order_by, descending,
  limit, offset, criteria_names) -> list[Row]`. Sorting is **allowlist-guarded**:
  a plain column must be in `ALLOWED_SORT_COLUMNS`; a `"criteria:<name>"` sort is
  validated against the rubric criteria (from `preferences.yaml`) before its JSON
  path is built server-side — a raw sort string is **never** interpolated into
  SQL. Sorting happens in SQLite via `json_extract` in the `ORDER BY`, with a
  NULLS-LAST idiom (`<expr> IS NULL, <expr> DESC`) so unscored rows sink to the
  bottom regardless of direction. `weekly_commute_fit` is a special case: it
  lives at `$.weekly_commute_fit` (top-level), not inside `$.criteria_scores`.
- `parse_json_column` / `hydrate_job` — decode the TEXT JSON columns once so
  templates stay dumb (no `json.loads` in Jinja).

### `storage/stats.py` — dashboard aggregates

`dashboard_stats(conn)` (funnel + per-stage pending counts), `last_run(conn)` /
`recent_runs(conn)` (parsed `runs` ledger). The "pending" counts **mirror the
`db.select_jobs_to_*` predicates exactly** — a test
(`tests/test_stats.py`) asserts each count equals `len(select_jobs_to_*)` on the
same DB, so the duplicated `WHERE` clauses can't silently drift.

## The app (`web/`)

- `settings.py` — resolves an explicit **project root** (walks up to
  `pyproject.toml`) so the server finds `data/jobs.db` + `preferences.yaml`
  regardless of the launch directory (the CLI's defaults are CWD-relative). Env
  overrides: `JOBSCOUT_DB_PATH`, `JOBSCOUT_PREFERENCES_PATH`.
- `app.py` — `create_app(settings)` factory (+ module-level `app` for the uvicorn
  import string). Caches the rubric criteria on `app.state`.
- `dependencies.py` — a **per-request** DB connection (WAL allows concurrent
  reads; sqlite3 connections aren't thread-safe to share).
- `routes.py`, `templating.py`, `templates/`, `static/`.

### Routes (all `GET`)

| Path | Renders |
|---|---|
| `/` | dashboard: agent-pipeline funnel + pending work + last run |
| `/offers` | ranked offer list (filter form + table) |
| `/offers/table` | just the `<table>` — the HTMX target for sort/filter swaps |
| `/offers/{id}` | offer detail (404 → friendly page) |
| `/healthz` | `{"ok": true}` liveness |

`/offers` and `/offers/table` share one `_table.html` partial, so first paint
and every HTMX sort/filter swap render identically. A bad `sort` param is a
`400` (not a silent fallback), so the UI can't drift into an un-sorted state
unnoticed.

### Pages

- **Dashboard** — the **Agent pipeline** section is live (total; by
  `filter_status`; scored; per-stage backlog; last-run card). A **My review**
  section is designed but hidden (`{% if false %}`), a drop-in slot for the V2
  application-tracking feature.
- **Offer list** — score / title / company / source / status badges / commute +
  remote chip / red-flag count. Sortable by the overall score **or any single
  rubric criterion** (best culture fit, best `weekly_commute_fit`, …) via the
  sort dropdown, plus status/source filters — all HTMX partial swaps.
- **Offer detail** — four blocks: summary + verdict (`company_brief.summary` +
  `score_json.reasoning` + red_flags); full score breakdown (each criterion's
  0–10 with its relative weight — weights sum to 73, shown as relative not /100);
  commute detail (best + the three strategies with per-leg breakdown + resolved
  address/source); company research + raw posting + metadata with an outbound
  "View original posting" link. A **hidden office-map slot** carries the office
  `lat`/`lon` for a future V2 map.

## Shutdown

`jobscout serve` (no `--reload`) drives a `uvicorn.Server` with a custom
`SIGINT`/`SIGTERM`/`SIGBREAK` handler that flips `should_exit`, plus a short
`timeout_graceful_shutdown` — so a single Ctrl+C tears the server down cleanly on
Windows (whose default uvicorn handling can miss an idle-loop Ctrl+C and orphan
the worker). `--reload` uses uvicorn's own supervisor, whose child handles Ctrl+C.

## Not in v1 (designed-in, deferred)

- **Application tracking (V2):** a separate `offer_review(job_id, disposition,
  applied_at, notes, updated_at)` table (kept out of the pipeline-owned `jobs`
  table); un-hides the "My review" dashboard section + adds list status controls.
  These become the **first write/POST routes** (and the first `python-multipart`
  dep).
- **Run-CLI buttons:** trigger pipeline stages (incl. targeted `score --ids`,
  which the backend already supports) from the UI via a background worker.
- **Company-location map:** wire the `#office-map` slot with a locally-vendored
  map + offline tiles (office coords only).
- **Dedicated LLM posting summary:** today the summary reuses existing
  scorer/company-brief output (no extra model call).

## Tests

`tests/test_queries.py` (read layer + the sort allowlist guard),
`tests/test_stats.py` (counts + the worklist-parity invariant),
`tests/test_web.py` (route smoke tests via FastAPI `TestClient` against a fixture
DB + `preferences.example.yaml`). All offline.

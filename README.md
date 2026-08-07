# Job Scout

A fully-local AI agent pipeline that collects Product Manager / Product
Owner offers in Île-de-France, scores them against your preferences, and
drafts tailored cover letters for the best ones. No cloud LLM APIs — inference
runs locally via Ollama. It catalogs, scores, and drafts; it never applies to
jobs or sends anything. A human reviews everything.

See `CLAUDE.md` for the rules of engagement, `docs/architecture.md` for how the
system is built, and `docs/job-scout-project-brief.md` for the full design
rationale and phase plan.

## Status

- ✅ **Phase 0** — setup + model bake-off (`qwen3.6:35b-a3b` chosen; see `scripts/bakeoff/README.md`).
- ✅ **Phase 1** — ingestion & state: three source adapters, SQLite storage, dedupe, idempotent runs.
- ✅ **Phase 2** — hard filters, LinkedIn description enrichment, address + commute enrichment (Google Routes, three commute strategies — see `docs/enrichment.md`), and LLM scoring (`qwen3.6:35b-a3b`, zero tools; the `weekly_commute_fit` sub-score is an owner-calibrated Python curve — see `docs/scoring.md`).
- 🚧 **Phase 3** (in progress) — the hand-rolled agent tool loop + the two **research agents** have shipped: address research (warm-up) and company research (a grounded company brief that feeds the scorer). The cover-letter agent is what remains. See `docs/agents.md`.
- ✅ **Webapp** — a local FastAPI + HTMX app (`jobscout serve`): dashboard / ranked offer list / offer detail (v1), plus application tracking and run-pipeline-from-the-UI buttons with a progress bar (v2). See `docs/webapp.md`.

See `docs/architecture.md` for what exists now and the current stage-by-stage status.

## Setup

```
uv sync
cp preferences.example.yaml preferences.yaml   # then edit with your own values
cp .env.example .env                            # then fill in real credentials
```

In `preferences.yaml`, set your **home address** under `home:` (or pin
`home.lat`/`home.lon`) — it's the commute origin. It stays in this gitignored
file and is sent only to the routing provider; commute *minutes*, never the
address, flow into scoring/digests/letters.

Drop your master cover letters and CV into `profile/` (see `profile/README.md`).

### Credentials (`.env`)

- **Welcome to the Jungle** — no credentials (public API).
- **France Travail** — register an app at [francetravail.io](https://francetravail.io),
  subscribe it to *Offres d'emploi v2*, and set `FRANCE_TRAVAIL_ID` / `FRANCE_TRAVAIL_SECRET`.
- **LinkedIn alert emails** — read-only IMAP over a dedicated folder:
  1. On the email account (Gmail recommended), enable 2-Step Verification and
     generate an **app-specific password**.
  2. Create a filter that labels incoming LinkedIn job alerts into a dedicated
     folder named `linkedin-alerts`.
  3. Point a LinkedIn saved-search job alert at that address.
  4. Set `IMAP_HOST=imap.gmail.com`, `IMAP_USER`, `IMAP_APP_PASSWORD`.

  The pipeline only ever reads this folder (never marks, moves, or deletes
  mail). It parses both native and forwarded alert emails.
- **Google Routes** (commute enrichment) — in a Google Cloud project with
  billing enabled, turn on the **Routes API**, create an API key, and set
  `GOOGLE_ROUTES_KEY`. The free tier easily covers job-hunt volume; set a budget
  cap so it can never charge. Validate it before a real run with
  `uv run python scripts/routes_smoke.py`. Geocoding uses **Base Adresse
  Nationale** (free, keyless — no credential needed). See `docs/enrichment.md`
  for why routing consolidated onto Google, and the home-address privacy rules.

## Usage

The pipeline runs as a sequence of commands, each reading the previous stage's
results from `data/jobs.db`. Every command is **idempotent** (a re-run only
processes offers not already handled) and has `--dry-run`.

**1. Ingest** new offers into `data/jobs.db`:

```
uv run jobscout ingest                          # all configured sources
uv run jobscout ingest --source wttj --limit 20 # one source, capped
uv run jobscout ingest --dry-run                # fetch + report, write nothing
```

"New since last run" = not in the DB. Prints a per-source summary (fetched /
new / seen-again); a dead source is reported failed without aborting the others.

**2. Filter** stored offers against the `preferences.yaml` hard filters
(contract, salary floor, seniority keywords, remote policy):

```
uv run jobscout filter              # judge newly-ingested offers
uv run jobscout filter --refilter   # re-judge all (after editing preferences)
```

Each offer gets a `passed` / `needs_review` / `rejected` verdict with reasons.

**3. Enrich LinkedIn descriptions** from the public guest endpoint (LinkedIn
alert emails carry no description). Rate-limited, cached, auto re-filters:

```
uv run jobscout enrich-linkedin --limit 25
```

**4. Research agents** — two hand-rolled agents (self-hosted SearXNG +
read-only page fetch), run **before** enrichment and scoring. Needs `SEARXNG_URL`
in `.env` and Ollama running. See `docs/agents.md`.

```
uv run jobscout research-address      # place the unroutable tail (bare "Paris" / unresolved)
uv run jobscout research-company      # grounded company brief for every offer (feeds the scorer)
uv run jobscout research-company --redo --limit 2   # re-research (or --dry-run to write nothing)
```

`research-address` places offers whose address is too vague to route (bare
"Paris") or otherwise unresolved, validating every agent address deterministically
(must geocode into Île-de-France) — a wrong address can never hard-reject an
offer. `research-company` builds a grounded company brief that feeds the scorer.
Both run before `enrich-commute` (the sole router), which then routes the
freshly-placed addresses.

**5. Enrich address + commute** for passed/needs_review offers — resolves the
office address (Base Adresse Nationale, or an agent-placed one from step 4) and
computes commute time via Google Routes as the fastest of three strategies
(transit / bike / rail+bike hybrid):

```
uv run jobscout enrich-commute              # enrich pending offers
uv run jobscout enrich-commute --re-enrich  # redo all (after editing home/commute prefs)
```

See `docs/enrichment.md` for the commute model and tunable bike bounds.

**6. LLM scoring** — score each surviving (`passed`/`needs_review`) offer against
the `preferences.yaml` rubric via local Ollama (with the company brief as
advisory context, when present).

```
uv run jobscout score                 # score offers not yet scored
uv run jobscout score --dry-run --limit 2   # call the model, write nothing
uv run jobscout score --rescore       # redo all (after editing the rubric)
uv run jobscout score --commute-only  # recompute only weekly_commute_fit + total, NO LLM call
uv run jobscout score --ids 61 75     # score only these job ids (composes with --commute-only)
```

The LLM (`qwen3.6:35b-a3b`, zero tools) scores the qualitative criteria and infers
`onsite_days`; the `weekly_commute_fit` sub-score is a deterministic,
owner-calibrated Python curve over `commute_minutes × 2 × onsite_days`, blended
into a normalized 0–100 total. Unknown commutes are flagged, never zeroed. See
`docs/scoring.md`.

When a commute changes for already-scored offers (e.g. the address agent placed
an office, then `enrich-commute` routed it), `--commute-only` folds the new
commute into the total with **no model call** — it preserves the LLM's
qualitative scores + reasoning (the model never sees commute, so re-running it
would only add noise). `--ids` scopes any score run to specific offers.

**7. The webapp** — a local UI over `data/jobs.db`: a stats dashboard, a
sortable/filterable ranked offer list, and a per-offer detail page (verdict, full
score breakdown, commute detail, company brief + original posting). It also
**tracks your own hunt** (mark each offer to-review / applied / not-interested +
notes) and **runs pipeline stages from the UI** — any stage or the whole pipeline
from the dashboard, or any applicable stage on a single offer from its detail page
(re-search an address, re-route a commute, rescore) — via a background runner with
a progress bar and a **Stop** button that cancels an in-progress run cleanly (the
next run resumes where it left off — handy for freeing the GPU mid-run).

```
uv run jobscout serve                 # → http://127.0.0.1:8020  (Ctrl+C to stop)
uv run jobscout serve --port 9000     # a different port
uv run jobscout serve --reload        # auto-reload on code changes (development)
```

FastAPI + HTMX, no build step, no CDN (htmx is vendored locally). The run buttons
execute the **same** pipeline code the CLI does — nothing new leaves the machine,
no email. Write routes are same-origin-guarded; the server binds `127.0.0.1`
only. The offer list sorts by the overall score or any individual rubric
criterion (best culture fit, best commute fit, …), and filters by a **posted-date
picker** (defaults to the last 30 days; undated LinkedIn offers are shown unless
you hide them, never silently dropped) — the default view also hides rejected
offers. See `docs/webapp.md`.

## Tests

```
uv run pytest              # offline unit tests (parsing, dedupe, idempotency)
uv run ruff check src tests
```

Fast smoke checks (no real server / no network): `uv run python
scripts/webapp_smoke.py` drives the webapp routes in-process.

For a hands-on end-to-end walkthrough against the live sources, see the manual
test plans: `docs/phase-1-manual-test-plan.md` (ingestion & state) and
`docs/phase-3-manual-test-plan.md` (the research agents + SearXNG wiring).

**Working in this repo?** See `docs/dev.md` for the dev workflow (running/stopping
the server, the branch→PR→merge flow, the Windows `serve --stop` gotcha).

## Model bake-off

To (re-)compare local models on JSON reliability and letter-drafting quality,
see `scripts/bakeoff/README.md`.

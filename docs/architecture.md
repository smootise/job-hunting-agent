# Architecture (as-built)

A living map of how Job Scout actually works, updated as each phase lands. For
the *why* and the full phase plan, see `job-scout-project-brief.md`; for the
rules of engagement, `CLAUDE.md`. This doc is the *what exists now*.

## The pipeline

```
ingest → normalize → dedupe → hard filters → research (address + company)
       → enrich (address + commute) → LLM scoring → [human threshold]
       → cover-letter agent → digest
```

The plumbing (ingest, dedupe, filters, enrichment, digest) is deterministic
Python. Only two stages touch the LLM: a zero-tool scoring call, and two small
hand-rolled agents (address research, cover-letter drafting). No agent
framework in v1 — the loop is readable on purpose.

**Status by stage:**

| Stage | Status | Where |
|---|---|---|
| Ingest (3 sources) | ✅ Phase 1 | `adapters/`, `pipeline/ingest.py` |
| Normalize / dedupe / idempotent state | ✅ Phase 1 | `normalize.py`, `storage/db.py` |
| Hard filters | ✅ Phase 2 | `pipeline/filters.py`, `pipeline/salary.py`, `pipeline/filter_stage.py` |
| Enrich (LinkedIn descriptions) | ✅ Phase 2 | `adapters/linkedin_guest.py`, `pipeline/enrich_linkedin.py` |
| Enrich (address + commute) | ✅ Phase 2 | `enrich/{geocode,address,routing}.py`, `pipeline/enrich_commute.py` — see `docs/enrichment.md` |
| LLM scoring | ✅ Phase 2 | `pipeline/{scoring,commute_score,score_stage}.py` — see `docs/scoring.md` |
| Shared agent tool loop | ✅ Phase 3 | `agents/{loop,tools}.py` — see `docs/agents.md` |
| Address-research agent | ✅ Phase 3 | `agents/address_agent.py`, `pipeline/research_address.py` |
| Company-research agent | ✅ Phase 3 | `agents/company_agent.py`, `pipeline/research_company.py` |
| Cover-letter agent | ⏳ Phase 3 (next) | — |
| Webapp (dashboard / list / detail; + tracking & run buttons) | ✅ v1 + v2 | `web/`, `storage/{queries,stats}.py` + `offer_review` in `db.py` — see `docs/webapp.md` |
| Digest / scheduling / ops | ⏳ Phase 4 | — |

## Package layout

```
src/jobscout/
  models.py          JobRecord — the common shape every source emits
  normalize.py       shared language / dedupe-key / contract helpers
  config.py          preferences.yaml + .env loaders, credential getters
  adapters/          one module per source (wttj, france_travail, linkedin_email)
  storage/db.py      SQLite schema, idempotent upsert, dedupe, run ledger,
                     offer_review table + upsert_review (webapp v2 tracking)
  storage/queries.py display reads for the webapp (get_job, list_jobs + sort guard
                     + offer_review LEFT JOIN / disposition filter)
  storage/stats.py   dashboard aggregates (funnel + pending counts, run ledger,
                     review_stats)
  pipeline/ingest.py       ingest orchestration (fail-soft per source, dry-run)
  pipeline/filters.py      pure hard-filter logic + FilterVerdict
  pipeline/salary.py       free-text salary → annual-gross EUR range parser
  pipeline/filter_stage.py filter orchestration (idempotent, --refilter, dry-run)
  adapters/linkedin_guest.py  pure parser for the public guest job-posting page
  pipeline/enrich_linkedin.py LinkedIn description backfill (guest endpoint,
                              cached, rate-limited, fail-soft, auto re-filter)
  enrich/geocode.py    Base Adresse Nationale geocoder + IDF region check
  enrich/address.py    deterministic office-address resolution chain
  enrich/routing.py    Google Routes wrapper + three-strategy commute planner
  pipeline/enrich_commute.py  address+commute enrichment orchestration
  pipeline/scoring.py       pure scoring: prompt, JSON validation, weighted total
                            (+ Phase 3: company brief as advisory context)
  pipeline/commute_score.py the Python-owned weekly_commute_fit curve
  pipeline/score_stage.py   LLM-scoring orchestration (retry-once, fail-soft)
  agents/loop.py            hand-rolled ReAct tool loop (shared; the Phase 3 lesson)
  agents/tools.py           whitelisted web_search (SearXNG) + read-only fetch_page
  agents/address_agent.py   warm-up agent: company+city → validated IDF address
  agents/company_agent.py   company brief (WTTJ profile → agent → grounding pass)
  pipeline/research_address.py  address-agent orchestration (tail only, fail-soft)
  pipeline/research_company.py  company-research orchestration (all offers)
  web/               FastAPI + HTMX webapp: read views (v1) + application
                     tracking & run-from-UI buttons (v2). runner.py = the
                     single-worker background pipeline runner; security.py =
                     same-origin guard for write routes.
  cli.py             `jobscout` entry point
  llm/client.py      thin Ollama wrapper with full-interaction logging
```

## Storage

SQLite at `data/jobs.db` is the single source of truth. The `jobs` table holds
the record fields plus bookkeeping (`first_seen_at`, `last_seen_at`,
normalized dedupe keys, `dup_group`, `status`) and the Phase 2 hard-filter
verdict (`filter_status` = passed|needs_review|rejected, `filter_reasons` JSON,
`filtered_at`), the address+commute enrichment columns, and the LLM-scoring
verdict (`score_total`, `score_status`, `score_json`, `scored_at`); a `runs`
table is the audit ledger. Schema is created
idempotently on connect, and newer columns are backfilled on an existing DB via
an `ALTER TABLE` guard (`_migrate_add_columns`), so a Phase 1 DB upgrades in
place. Markdown outputs (`output/digests/`, `output/letters/`) arrive later.

## Key invariants (enforced in code today)

- **Idempotent runs:** re-running inserts 0 new and only bumps `last_seen_at`
  (`storage/db.py`).
- **Wide net, filter later:** adapters filter only keywords + region; strict
  filters are deferred to Phase 2 so nothing is silently dropped.
- **Fully local, no email-send:** no cloud LLM calls, no SMTP anywhere.
- **Read-only IMAP** for LinkedIn; **home address never in prompts/logs**
  (Phase 2 concern, but the rule is in force).

## Current state & next: Phase 3 (letter agent remaining)

**Phase 2 is complete**, and the **two Phase 3 research agents have shipped**
(the cover-letter agent is what remains of Phase 3). Phase 2: **hard filters**
(`pipeline/filters.py` + `salary.py` + `filter_stage.py`, CLI `jobscout filter`),
**LinkedIn description enrichment** (`adapters/linkedin_guest.py` +
`pipeline/enrich_linkedin.py`, CLI `jobscout enrich-linkedin`), **address +
commute enrichment** (`enrich/{geocode,address,routing}.py` +
`pipeline/enrich_commute.py`, CLI `jobscout enrich-commute` — full detail in
`docs/enrichment.md`), and **LLM scoring**
(`pipeline/{scoring,commute_score,score_stage}.py`, CLI `jobscout score` — full
detail in `docs/scoring.md`).

**Phase 3 so far — the shared tool loop + two research agents** (full detail in
`docs/agents.md`): a hand-rolled ReAct loop (`agents/loop.py`) drives two agents
with two read-only whitelisted tools (`agents/tools.py`: `web_search` via
self-hosted SearXNG + `fetch_page`). `jobscout research-address` runs the
**address-research agent** (warm-up) on the unroutable tail
(`needs_address`/`unresolved`), validates its candidate deterministically (BAN
geocode + Île-de-France), and stores an `address_source='agent'` address —
leaving routing to `enrich-commute`. `jobscout research-company` runs the
**company-research agent** on every passed/needs_review offer: a deterministic
WTTJ-profile fetch, then agent gap-fill, then a **grounding-verification pass**
that strips unsupported claims, producing a `company_brief` JSON blob. Both run
**before** `enrich-commute` and `score` in the pipeline; the brief feeds the
zero-tool scorer as delimiter-wrapped **advisory context** (never a hard gate).

Two deviations from the brief's original Phase 3 plan, decided with the owner and
recorded in `docs/agents.md`: (1) the address agent was **widened into two
agents** — the warm-up address agent *plus* a company-research agent that
enriches *all* offers (not just missing-address ones) with a grounded company
brief for the scorer; (2) the deterministic **WTTJ-profile and French-registry**
resolution steps the brief listed were **not** built as address pre-filters — the
registry returns an HQ address (a weak fit for the bare-"Paris" worklist), so
they're deferred as an optional future pre-filter; the WTTJ profile instead feeds
the *company* brief deterministically.

**Webapp (shipped, alongside Phase 3).** A local FastAPI + HTMX UI
(`jobscout serve`) over `data/jobs.db`, in two layers. **v1 (read):** a stats
dashboard, a ranked offer list sortable by the overall score or any single rubric
criterion, and a per-offer detail page — mounted on `storage/queries.py`
(allowlist-guarded display reads) and `storage/stats.py` (dashboard counts that
mirror the pipeline worklists). **v2 (writes):** *application tracking* — a
separate `offer_review` table (human-owned; `upsert_review` preserves the
original applied date), surfaced as a "My review" dashboard section, a detail-page
disposition/notes control, and a list filter; and *run-from-UI buttons* — a
single-worker background runner (`web/runner.py`, started via a lifespan) that
runs the **same `pipeline.run_*` code** the CLI does, serialized (one SQLite
writer), with a per-offer progress bar (an additive `on_progress` param on every
`run_*`). The dashboard runs whole-worklist stages or the whole pipeline
(halt-on-raise, resume-on-re-press via idempotency); the detail page re-runs any
applicable stage on **one offer** (`POST /offers/{id}/runs/{stage}`, forcing past
the done-gate but keeping eligibility gates). All write routes are same-origin
guarded (`web/security.py`); the runner adds no new external capability and sends
no email. Full detail in `docs/webapp.md`.

The scorer gained two targeting flags the webapp drives (and useful for ad-hoc
runs): **`score --ids ID ...`** (score only specific offers — the primitive
generalized across every stage's selector for the per-offer buttons) and
**`score --commute-only`** (recompute only the Python `weekly_commute_fit` +
total from the stored breakdown, **no LLM call** — folds a changed commute into
an already-scored offer without re-rolling the model's qualitative judgment). See
`docs/scoring.md`.

**Next up:** the cover-letter agent (reuses `agents/loop.py` + `fetch_page` in
`hard_whitelist` mode + one sandboxed `save_draft`).

The two decisions below were made during the filter/enrichment work and are now
honored by the shipped scorer — kept here as the decision record.

On the real 144-offer DB the filter stage yields ~72 passed / ~33 needs_review /
~39 rejected; commute enrichment ran on the 105 passed+needs_review, of which a
live run enriched ~72 (18 remote → commute 0), flagged 3 bare-Paris as `needs_address`,
and left ~30 retryable (unresolved LinkedIn locations / uncgeocodable cities).

**LLM scoring (shipped)** — for the same **`passed` + `needs_review`** scope
enrichment used (the scorer owns the needs_review tail — decision #1 below),
`jobscout score` calls `qwen3.6:35b-a3b` (zero tools) with the `preferences.yaml`
rubric + the offer and stores schema-validated JSON: per-criterion scores, a
weighted total 0–100, one-paragraph reasoning, `red_flags[]`. Weights are
normalized (they sum to **75**, not 100); the stage retries once on invalid JSON
then marks needs_review; the untrusted posting is delimiter-wrapped; every call
is logged via `llm/client.py`. It mirrors the established stage pattern
(idempotent by `scored_at`, `--dry-run` / `--limit` / `--rescore`, fail-soft per
row, additive columns). Full as-built detail — including the owner-calibrated
commute curve — is in **`docs/scoring.md`**.

The two decisions from the filter/enrichment work the scorer honors:

1. **The scorer owns remote-policy inference for the silent tail.** The
   deterministic `classify_remote_policy` reads location + description prose and
   resolves most offers, but ~33 France Travail postings state no remote policy
   *anywhere* — confirmed real data absence (the FT API exposes **no** structured
   remote field; `contexteTravail` carries only work-hours). Those sit at
   `needs_review`. The scorer reads the full description and must infer likely
   onsite-days for `weekly_commute_fit` anyway, so it is the right place to
   judge the remote policy of these silent offers — not more regex.

2. **`weekly_commute_fit` gets `commute_minutes` from enrichment (now built).**
   `jobscout enrich-commute` writes a headline `commute_minutes`/`commute_mode`
   (fastest of three strategies) plus a `commute_strategies` JSON blob to each
   passed/needs_review row (`docs/enrichment.md`). **Decision made at the owner's
   request: `weekly_commute_fit` is computed in Python (a deterministic curve
   over `commute_minutes × 2 × onsite_days`), NOT by the LLM** — so a better
   address or a changed threshold rescoring is pure math, no LLM call. The LLM
   still infers `onsite_days` from the posting and scores the qualitative
   criteria.

   The commute value a scoring run will find, by `address_source`:
   - `remote` → `commute_minutes = 0` (max score on the criterion).
   - `posting`/`wttj`/`france_travail`/`linkedin_email` → a real routed number
     (`address_confidence` high/medium). `approximate` (out-of-IDF centroid,
     confidence `low`) is a *usable but estimated* number — score it, flag it.
   - `needs_address` (bare "Paris", too vague) and NULL commute (routing failed /
     `unresolved`) → **no usable commute.** Score `weekly_commute_fit` with the
     criterion **flagged / assumption-noted, never zeroed** (a NULL is "unknown",
     not "0 minutes"). These are the same rows the Phase 3 address agent will
     later sharpen, after which `--rescore` recomputes the Python curve for free.

Contract note for the scorer's inputs: an offer may carry a `filter_reasons`
entry "contract type not stated; assumed CDI" (from `assume_cdi_when_unstated`).
That's an *assumption*, not a stated fact — surface it, don't treat as certain.

## Deeper references

- **Ingestion detail + API quirks:** `docs/ingest.md`
- **Address + commute enrichment:** `docs/enrichment.md`
- **LLM scoring + the commute curve:** `docs/scoring.md`
- **Phase 3 agents (loop, tools, both agents, SearXNG setup):** `docs/agents.md`
- **Manual test plan:** `docs/phase-1-manual-test-plan.md`
- **Model choice (bake-off):** `scripts/bakeoff/README.md`

# Architecture (as-built)

A living map of how Job Scout actually works, updated as each phase lands. For
the *why* and the full phase plan, see `job-scout-project-brief.md`; for the
rules of engagement, `CLAUDE.md`. This doc is the *what exists now*.

## The pipeline

```
ingest → normalize → dedupe → hard filters → enrich (address + commute)
       → LLM scoring → [human threshold] → cover-letter agent → digest
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
| LLM scoring | ⏳ Phase 2 (next) | — |
| Address-research agent | ⏳ Phase 3 | — |
| Cover-letter agent | ⏳ Phase 3 | — |
| Digest / scheduling / ops | ⏳ Phase 4 | — |

## Package layout

```
src/jobscout/
  models.py          JobRecord — the common shape every source emits
  normalize.py       shared language / dedupe-key / contract helpers
  config.py          preferences.yaml + .env loaders, credential getters
  adapters/          one module per source (wttj, france_travail, linkedin_email)
  storage/db.py      SQLite schema, idempotent upsert, dedupe, run ledger
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
  cli.py             `jobscout` entry point
  llm/client.py      thin Ollama wrapper with full-interaction logging
```

## Storage

SQLite at `data/jobs.db` is the single source of truth. The `jobs` table holds
the record fields plus bookkeeping (`first_seen_at`, `last_seen_at`,
normalized dedupe keys, `dup_group`, `status`) and the Phase 2 hard-filter
verdict (`filter_status` = passed|needs_review|rejected, `filter_reasons` JSON,
`filtered_at`); a `runs` table is the audit ledger. Schema is created
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

## Current state & next: LLM scoring

Done in Phase 2 so far: **hard filters** (`pipeline/filters.py` + `salary.py` +
`filter_stage.py`, CLI `jobscout filter`), **LinkedIn description enrichment**
(`adapters/linkedin_guest.py` + `pipeline/enrich_linkedin.py`, CLI
`jobscout enrich-linkedin`), and **address + commute enrichment**
(`enrich/{geocode,address,routing}.py` + `pipeline/enrich_commute.py`, CLI
`jobscout enrich-commute` — full detail in `docs/enrichment.md`). On the real
144-offer DB the filter stage yields ~72 passed / ~33 needs_review / ~39
rejected; commute enrichment then runs on the ~105 passed+needs_review.

**Next stage is LLM scoring** — for `filter_status='passed'` (and arguably
`needs_review`) offers, call `qwen3.6:35b-a3b` (zero tools) with the
`preferences.yaml` rubric + the offer, and store schema-validated JSON: per-
criterion scores, weighted total 0–100, one-paragraph reasoning, `red_flags[]`.
See CLAUDE.md's "scoring_rubric" for the exact semantics (normalize weights —
they sum to 73 not 100; retry once on invalid JSON then mark needs_review; wrap
the untrusted posting in delimiters; log every call in full via `llm/client.py`).

Two decisions from the filter/enrichment work that the scorer must honor:

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
   criteria. Fully-remote offers already carry `commute_minutes = 0`. Offers with
   a NULL commute (routing failed / address unresolved) should be scored with the
   commute criterion flagged, not zeroed. Note `address_confidence='low'` /
   `address_source='approximate'` marks an estimated commute for review.

Contract note for the scorer's inputs: an offer may carry a `filter_reasons`
entry "contract type not stated; assumed CDI" (from `assume_cdi_when_unstated`).
That's an *assumption*, not a stated fact — surface it, don't treat as certain.

## Deeper references

- **Ingestion detail + API quirks:** `docs/ingest.md`
- **Manual test plan:** `docs/phase-1-manual-test-plan.md`
- **Model choice (bake-off):** `scripts/bakeoff/README.md`

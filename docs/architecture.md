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
| Hard filters | ⏳ Phase 2 | — |
| Enrich (address + commute) | ⏳ Phase 2 | — |
| LLM scoring | ⏳ Phase 2 | — |
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
  pipeline/ingest.py orchestration (fail-soft per source, dry-run)
  cli.py             `jobscout` entry point
  llm/client.py      thin Ollama wrapper with full-interaction logging
```

## Storage

SQLite at `data/jobs.db` is the single source of truth. The `jobs` table holds
the record fields plus bookkeeping (`first_seen_at`, `last_seen_at`,
normalized dedupe keys, `dup_group`, `status`); a `runs` table is the audit
ledger. Schema is created idempotently on connect. Markdown outputs
(`output/digests/`, `output/letters/`) arrive in later phases.

## Key invariants (enforced in code today)

- **Idempotent runs:** re-running inserts 0 new and only bumps `last_seen_at`
  (`storage/db.py`).
- **Wide net, filter later:** adapters filter only keywords + region; strict
  filters are deferred to Phase 2 so nothing is silently dropped.
- **Fully local, no email-send:** no cloud LLM calls, no SMTP anywhere.
- **Read-only IMAP** for LinkedIn; **home address never in prompts/logs**
  (Phase 2 concern, but the rule is in force).

## Deeper references

- **Ingestion detail + API quirks:** `docs/ingest.md`
- **Manual test plan:** `docs/phase-1-manual-test-plan.md`
- **Model choice (bake-off):** `scripts/bakeoff/README.md`

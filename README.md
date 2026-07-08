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
- ⏳ **Phase 2** — hard filters, address/commute enrichment, LLM scoring (next).

## Setup

```
uv sync
cp preferences.example.yaml preferences.yaml   # then edit with your own values
cp .env.example .env                            # then fill in real credentials
```

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

## Usage

Ingest new offers into `data/jobs.db`:

```
uv run jobscout ingest                          # all configured sources
uv run jobscout ingest --source wttj --limit 20 # one source, capped
uv run jobscout ingest --dry-run                # fetch + report, write nothing
```

Runs are **idempotent** — re-running only records offers not already stored
("new since last run" = not in the DB). The command prints a per-source
summary (fetched / new / seen-again). A dead source is reported as failed
without aborting the others.

## Tests

```
uv run pytest              # offline unit tests (parsing, dedupe, idempotency)
uv run ruff check src tests
```

For a hands-on end-to-end walkthrough against the live sources, see
`docs/phase-1-manual-test-plan.md`.

## Model bake-off

To (re-)compare local models on JSON reliability and letter-drafting quality,
see `scripts/bakeoff/README.md`.

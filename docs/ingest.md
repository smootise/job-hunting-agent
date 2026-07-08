# Ingestion — as-built reference (Phase 1)

Read this before changing anything under `adapters/`, `normalize.py`,
`storage/db.py`, or `pipeline/ingest.py`. It records how ingestion actually
works and the API quirks that took live probing to discover — so you don't
re-derive them. The *design* rationale is in `job-scout-project-brief.md`;
this is the *implementation reality*.

## Flow

```
adapter.fetch()  ->  list[JobRecord]        (per source; pure producers, no DB)
        │
   pipeline/ingest.run_ingest()
        │  concat, drop intra-batch (source, external_id) dups
        ▼
   storage/db.upsert_jobs()                 (insert new / bump last_seen_at)
        ▼
   data/jobs.db                             (single source of truth)
```

Every adapter emits the identical `JobRecord` shape (`models.py`), so
everything downstream is source-agnostic. Adapters never touch the DB — the
pipeline persists. `run_ingest` is fail-soft per source (one dead source is
reported `FAILED`, the rest still ingest) and honors `--dry-run` (fetch +
count, write nothing).

## The record shape

`JobRecord` (frozen dataclass, `models.py`): `source, external_id, url, title,
company, location, contract_type, salary_text, description, posted_at, lang`.
Missing fields are `None`, never dropped. `(source, external_id)` is the
primary dedupe key.

## Idempotency & dedupe (the Phase 1 lesson)

- **"New since last run" = "not already in `data/jobs.db`."** Sources are
  re-fetched in full every run; novelty is decided by the DB, enforced by a
  `UNIQUE(source, external_id)` constraint.
- `upsert_jobs` inserts new rows and, for a known `(source, external_id)`,
  **only bumps `last_seen_at`** — never rewrites stored content. Re-running
  the same batch inserts 0. This is the invariant; don't break it.
- **Cross-source dedupe is soft:** the same job on two sources gets a shared
  `dup_group` (matched on normalized company+title) but **keeps both physical
  rows** — each source's URL/description is distinct and worth keeping.
  Embedding-based similarity is explicitly v2; the v1 matcher is normalized
  string equality (won't merge reworded titles, but also won't falsely merge
  unrelated ones — the safer error).
- The `runs` table is an audit ledger: one row per run with per-source counts;
  dry-runs never write to it.

## Normalization (`normalize.py`)

- `detect_language` — French/English stopword + diacritic heuristic; defaults
  to `fr`. Drives which master letter the Phase 3 letter agent uses.
- `normalize_company` / `normalize_title` — produce **dedupe keys only**
  (strip legal suffixes, gender markers `(H/F)`, punctuation). Never display
  these; the originals stay on the record. Seniority keyword matching (Phase 2)
  runs on the *original* title with whole-token boundaries, never on these.
- `parse_contract_type` — maps native vocab to `CDI`/`CDD`/`interim`/… and
  returns **`None` when unstated** (e.g. WTTJ's `full_time`, which is a
  schedule not a contract). `None` means *needs_review* downstream — never a
  silent reject.

## Filtering philosophy (why adapters over-fetch on purpose)

Adapters filter **only on keywords + region**. Every strict filter (CDI,
salary floor, seniority, commute) is deferred to Phase 2, where rejections are
logged. An opaque API-side filter that misclassifies one posting would kill a
good offer invisibly — the worst outcome in this project. So the DB
deliberately contains CDD/interim/internship offers; Phase 2 rejects them
transparently.

## Source adapters — quirks that cost live probing

### WTTJ (`adapters/wttj.py`)
- Public Algolia index (`wk_cms_jobs_production`), search-only key shipped in
  the WTTJ frontend — **not a secret**, safe to inline. Rotation: watch the
  `/queries` XHR on the live site and copy the new `x-algolia-api-key`.
- **Auth goes in the query string** (`x-algolia-application-id` /
  `x-algolia-api-key`), not headers.
- **A `Referer: https://www.welcometothejungle.com/` header is required** — the
  key enforces a referer allowlist; without it you get
  `403 "Method not allowed with this referer"`.
- Region filter: Algolia facet `offices.state:Ile-de-France`.
- Contract: read `contract_type_names.fr` (e.g. `CDI`), **not** the raw
  `contract_type` (which is the schedule, `FULL_TIME`).
- Search hits carry **no full description** (`description` is `None`); Algolia
  also returns each hit **duplicated within a page** — intra-batch dedupe
  handles it.

### France Travail (`adapters/france_travail.py`)
- OAuth2 `client_credentials`, `realm=/partenaire`, scope
  `api_offresdemploiv2 o2dsoffre`. App must be **subscribed to Offres d'emploi
  v2** or the token works but search 403s.
- Region: **`region=11`** (Île-de-France) — one param for all 8 departments.
  A comma-joined `departement` list is not the reliable path.
- Pagination: `range=start-end`, **span must be ≤ 150** (else
  `400 "plage trop importante"`). The `Content-Range: offres 0-N/TOTAL`
  header gives the stop total. Search returns **206** (partial) on ranged
  results — that's success, not an error; `204` = empty.
- URL is `origineOffre.urlOrigine`; salary is `salaire.libelle` or the free-text
  `commentaire` (`Selon profil`, `N/A` → treated as no stated salary).

### LinkedIn alert emails (`adapters/linkedin_email.py`)
- **Read-only IMAP** (`imaplib`), folder opened `readonly=True` (EXAMINE mode)
  — fetching doesn't even set `\Seen`. Never flag/move/delete mail.
- Default folder `linkedin-alerts` (a Gmail label exposed as an IMAP folder).
- `parse_alert_email(raw)` is **pure** (unit-tested against a saved `.eml`).
  It picks the `text/html` MIME part and works for **both native and
  forwarded** alerts (the forwarded copy still nests LinkedIn's HTML, so
  picking the HTML part is all the "unwrapping" needed).
- Parsing: find each `/jobs/view/{id}` anchor (→ `external_id` + URL), climb to
  the enclosing table row whose text reads `title` / `Company · Location
  (policy)`. A job appears in several anchors (thumbnail + text) → dedupe by id.
- Alerts state **no contract and no salary** → both `None` (needs_review).

## Credentials (`config.py`)

- Hand-rolled `.env` reader (no `python-dotenv`); real `os.environ` overrides
  file values. Secrets are read here and passed explicitly to adapters —
  never logged, never in an LLM prompt.
- `france_travail_credentials()` / `imap_credentials()` raise a clear
  "set X in .env" error when a source needing them is enabled. WTTJ needs none.

## Tests & fixtures (`tests/`)

- Fully offline: parsers tested against captured fixtures
  (`wttj_search.json`, `france_travail_search.json`) and scrubbed synthetic
  `.eml` files. Idempotency/dedupe tested on in-memory SQLite.
- The **real** captured LinkedIn `.eml` (`linkedin_alert_sample.eml`) is
  gitignored (it contains a personal address); only `*_synthetic.eml` is
  committed. See `.gitignore`.
- Manual end-to-end checklist: `docs/phase-1-manual-test-plan.md`.

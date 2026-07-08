# Phase 1 — Manual Test Plan

A hands-on checklist to verify the ingestion & state layer yourself. Work top
to bottom; each step says **what to run**, **what you should see**, and **why
it matters**. Check the box when it passes.

> All commands are run from the repo root in a shell (PowerShell or Git Bash).
> `uv` must be installed and `.env` filled in (FT creds + Gmail app password).

---

## 0. Prerequisites

- [ ] `.env` exists at the repo root with `FRANCE_TRAVAIL_ID`,
      `FRANCE_TRAVAIL_SECRET`, `IMAP_HOST=imap.gmail.com`, `IMAP_USER`,
      `IMAP_APP_PASSWORD` filled in.
- [ ] The Gmail `linkedin-alerts` folder contains at least one LinkedIn job
      alert (forwarded is fine).

```bash
uv sync
```
**Expect:** resolves and installs, including `pytest` and `beautifulsoup4`.
**Why:** confirms the environment builds from `pyproject.toml` alone.

---

## 1. Automated suite (offline, no network)

```bash
uv run pytest -v
```
- [ ] **24 tests pass**, 0 failures.
- [ ] No test makes a network call (they run in well under a second).

**Why it matters:** these lock in the logic that fails *silently* if wrong —
language detection, dedupe keys, the unstated-contract→`None` rule, and the
core idempotency guarantee. Reproducible because they run against captured
fixtures, not live APIs.

**Spot-check individual concerns:**
```bash
uv run pytest tests/test_dedupe.py -v      # idempotency + cross-source grouping
uv run pytest tests/test_normalize.py -v   # language + normalization traps
uv run pytest tests/test_adapters.py -v    # each parser vs. its fixture
uv run pytest tests/test_db.py -v          # schema, run ledger, dry-run
```

---

## 2. Lint

```bash
uv run ruff check src/ tests/
```
- [ ] **"All checks passed!"**

---

## 3. CLI smoke test

```bash
uv run jobscout
```
- [ ] Prints help listing the `ingest` subcommand.

```bash
uv run jobscout ingest --help
```
- [ ] Shows `--source` (choices: wttj, france_travail, linkedin_email),
      `--limit`, `--dry-run`.

---

## 4. Dry-run writes nothing (safety)

Make sure no DB exists first, then dry-run:
```bash
rm -rf data/            # Git Bash;  PowerShell: Remove-Item -Recurse -Force data -EA SilentlyContinue
uv run jobscout ingest --dry-run --source wttj --limit 10
```
- [ ] Prints a table with `fetched` populated, `new`/`seen_again` shown as `-`.
- [ ] Header says **"(dry-run - nothing written)"**.

Confirm nothing was persisted:
```bash
ls data/ 2>/dev/null || echo "no data dir — correct"
```
- [ ] `data/jobs.db` was **NOT** created.

**Why it matters:** `--dry-run` must be a pure read. This proves the only
write path is the real upsert.

---

## 5. Live ingest — WTTJ (no credentials needed)

```bash
uv run jobscout ingest --source wttj --limit 20
```
- [ ] `wttj` row shows a non-zero `fetched` and `new` (e.g. ~13–20), status `ok`.
- [ ] `data/jobs.db` now exists.

**Why it matters:** proves the public Algolia path works end to end, including
the Referer-header requirement that otherwise 403s.

---

## 6. Idempotency — the headline guarantee

Run the **exact same command again, immediately**:
```bash
uv run jobscout ingest --source wttj --limit 20
```
- [ ] `new` is now **0**.
- [ ] `seen_again` equals what `new` was in step 5.

**Why it matters:** "new since last run" = "not already in the DB". Re-running
must never duplicate rows — this is the core Phase 1 lesson. If `new` is
non-zero on the second run, idempotency is broken.

---

## 7. Live ingest — France Travail (OAuth)

```bash
uv run jobscout ingest --source france_travail --limit 30
```
- [ ] `france_travail` row shows non-zero `fetched`/`new`, status `ok`.
- [ ] Re-run it → `new` drops to 0 (idempotent per source too).

**Why it matters:** proves the OAuth2 client-credentials flow + Offres
d'emploi v2 search + the `range`≤150 pagination all work with your creds.

**If it FAILS with a 403 on search:** your francetravail.io app isn't
subscribed to *Offres d'emploi v2*. Add the subscription in the portal.

---

## 8. Live ingest — LinkedIn (read-only IMAP)

```bash
uv run jobscout ingest --source linkedin_email --limit 50
```
- [ ] `linkedin_email` row shows the jobs parsed out of your alert emails.
- [ ] Re-run → `new` is 0.

Verify it was truly read-only (nothing in Gmail changed):
- [ ] Open Gmail → `linkedin-alerts`: the alert emails are **still unread if
      they were unread**, none deleted, none moved.

**Why it matters:** the IMAP folder is opened in EXAMINE (read-only) mode —
the pipeline must never flag, move, or delete your mail.

---

## 9. All sources together + inspect the data

```bash
uv run jobscout ingest --limit 60
```
- [ ] All three source rows appear; totals add up.

Inspect the stored data (needs the `sqlite3` CLI, or use any SQLite viewer):
```bash
sqlite3 data/jobs.db "SELECT source, COUNT(*) FROM jobs GROUP BY source;"
sqlite3 data/jobs.db "SELECT COALESCE(contract_type,'(needs_review)'), COUNT(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC;"
sqlite3 data/jobs.db "SELECT source, title, company FROM jobs LIMIT 10;"
```
- [ ] Counts look sane per source.
- [ ] **Contract types include CDI *and* others** (CDD, internship, interim,
      `(needs_review)`). This is correct and intentional — see below.
- [ ] Titles/companies are readable (not garbled, accents preserved).

**Why the mix of contract types matters:** we deliberately do NOT filter
contract type at the API. A silent API-side filter could drop a CDI-implied
posting. Phase 2 will reject non-CDI transparently, with a logged reason. If
you *only* saw CDI here, that would mean an API filter is silently dropping
offers — the opposite of what we want.

---

## 10. Run ledger (auditability)

```bash
sqlite3 data/jobs.db "SELECT id, dry_run, substr(source_counts_json,1,80) FROM runs ORDER BY id;"
```
- [ ] One row per real run you executed, each with `finished_at` set and
      per-source counts recorded. `dry_run` runs are not here (they don't
      touch the DB).

**Why it matters:** every run is auditable after the fact.

---

## 11. Fail-soft (one dead source doesn't sink the run)

Temporarily break credentials to simulate an outage:
```bash
# Git Bash:
FRANCE_TRAVAIL_SECRET=wrong uv run jobscout ingest --source france_travail --source wttj --limit 10
```
```powershell
# PowerShell:
$env:FRANCE_TRAVAIL_SECRET="wrong"; uv run jobscout ingest --source france_travail --source wttj --limit 10; Remove-Item Env:\FRANCE_TRAVAIL_SECRET
```
- [ ] `france_travail` row shows **FAILED** with an error message.
- [ ] `wttj` row still shows `ok` and ingests normally.

**Why it matters:** a single source going down (API outage, expired token)
must not abort the whole run. The others still ingest.

---

## 12. Security & privacy invariants (eyeball checks)

- [ ] **Home address never in code/logs:** `preferences.yaml` and `.env` are
      the only places personal location lives, and both are gitignored.
      ```bash
      git check-ignore preferences.yaml .env
      ```
      Both should print (= both ignored).
- [ ] **Real email is not committed:** the captured alert stays local.
      ```bash
      git check-ignore tests/fixtures/linkedin_alert_sample.eml
      ```
      Should print the path (= ignored). The `*_synthetic.eml` files are the
      only emails in git.
- [ ] **No secrets in the diff:** skim the pushed commit — no API keys,
      passwords, or your address appear.
      ```bash
      git show --stat HEAD
      ```
- [ ] **No email-send capability anywhere:** there is no SMTP/send code in the
      repo, by design.
      ```bash
      grep -ri "smtplib\|sendmail\|send_message\|SMTP" src/ || echo "none — correct"
      ```

---

## Sign-off

- [ ] Steps 1–12 all pass.
- [ ] You understand *why* the contract-type mix in step 9 is intentional.
- [ ] Idempotency (steps 6–8) held for every source.

If all boxes are checked, Phase 1 (ingestion & state) is verified. Phase 2
adds the hard filters, address/commute enrichment, and LLM scoring on top of
this stored data.

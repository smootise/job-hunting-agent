# Phase 3 — Manual Test Plan (research agents)

A hands-on checklist to verify the two research agents (address + company) and
their shared tool loop yourself. Work top to bottom; each step says **what to
run**, **what you should see**, and **why it matters**. Check the box when it
passes.

> All commands run from the repo root in a shell (PowerShell or Git Bash).
> `uv` must be installed. Phase 1 + 2 must already be verified — this plan
> assumes `data/jobs.db` exists with **filtered** offers in it (run
> `jobscout ingest` then `jobscout filter` first if not). Full design detail is
> in `docs/agents.md`; read it once before starting.

This phase adds two invariants worth keeping front of mind while you test:
- **A wrong address can never hard-reject an offer** — the agent's address is
  validated deterministically (must geocode into Île-de-France) *outside* the
  model; a bad one falls back to the city centroid, never a rejection.
- **The company brief is advisory only** — it reaches a zero-tool scorer as
  clearly-labeled untrusted context, never a hard gate.

---

## 0. Prerequisites

- [ ] Phase 1 + 2 verified; `data/jobs.db` has offers with `filter_status` set
      (`passed`/`needs_review`/`rejected`). Quick check:
      ```bash
      sqlite3 data/jobs.db "SELECT filter_status, COUNT(*) FROM jobs GROUP BY 1;"
      ```
      You should see non-zero `passed` and `needs_review` rows.
- [ ] **SearXNG is up on your LAN** and its JSON API answers:
      ```bash
      curl "http://192.168.1.63:8085/search?q=test&format=json"
      ```
      Returns JSON with a `results` array. A **403** means `format=json` isn't
      enabled in SearXNG settings (`search.formats: [html, json]` — see
      `docs/agents.md`); a **connection error** means the container/port is down.
- [ ] `.env` contains `SEARXNG_URL=http://192.168.1.63:8085` (your NAS IP/port).
- [ ] **Ollama is running** with the model pulled:
      ```bash
      ollama list          # qwen3.6:35b-a3b should be listed
      ```

```bash
uv sync
```
**Expect:** resolves and installs (httpx, beautifulsoup4, pyyaml, pytest).
**Why:** confirms the environment builds from `pyproject.toml` alone.

---

## 1. Automated suite (offline, no network, no Ollama)

```bash
uv run pytest -q
```
- [ ] **273 tests pass**, 0 failures, in a few seconds.
- [ ] No test hits the network or Ollama (they use scripted models + mock HTTP).

**Spot-check the Phase 3 concerns individually:**
```bash
uv run pytest tests/test_agent_loop.py -v      # loop: tool cycle, forced-final, needs_review
uv run pytest tests/test_agent_tools.py -v     # web_search + fetch_page: policy, cache, guards
uv run pytest tests/test_address_agent.py -v   # the IDF validation net (the safety property)
uv run pytest tests/test_company_agent.py -v   # WTTJ profile + grounding pass (strip unsupported)
uv run pytest tests/test_research_stages.py -v # both stages end-to-end + brief→scorer
```
- [ ] Each file passes (13 / 13 / 10 / 13 / 7 = **56** Phase 3 tests).

**Why it matters:** these lock in the logic that fails *silently* if wrong — the
loop's stop conditions, the address validation gate, and the grounding strip.
Reproducible because they run against scripted models, never live inference.

---

## 2. Lint

```bash
uv run ruff check src/ tests/ scripts/
```
- [ ] **"All checks passed!"**

---

## 3. CLI smoke test

```bash
uv run jobscout
```
- [ ] Help lists `research-address` and `research-company` between
      `enrich-commute` and `score`.

```bash
uv run jobscout research-address --help
uv run jobscout research-company --help
```
- [ ] Each shows `--limit`, `--model` (default `qwen3.6:35b-a3b`), `--redo`,
      `--dry-run`.

---

## 4. SearXNG + agent wiring (the smoke scripts)

These run the **real** agent against your live SearXNG + Ollama on a known
company, so a wiring problem surfaces here with a clear message instead of deep
inside the pipeline. Run before any real stage.

```bash
uv run python scripts/research_address_smoke.py
```
- [ ] Prints your `SEARXNG_URL`, then `web_search OK — N results`.
- [ ] Runs the agent (a few model calls; the first is slow — Ollama loads the
      model into VRAM), prints an `agent candidate:` line.
- [ ] Ends with either `validated (IDF): <address>` **or** a clean "validation
      net rejected the candidate … a clean fallback outcome" message. **Both are
      passes** — one shows a positive resolution, one shows the safety net working.

```bash
uv run python scripts/research_company_smoke.py
```
- [ ] Prints the four steps (WTTJ profile fetch → draft → grounding), ending in a
      `grounded brief:` JSON block, or a `needs_review` message (also a pass).

**Try a company you know is in Paris** (positive path):
```bash
uv run python scripts/research_address_smoke.py "Doctolib"
```
- [ ] You get a real Île-de-France street address, confidence `medium`.

**Why it matters:** proves the whole chain — SearXNG JSON API, `fetch_page`,
the local model, JSON parsing, and the deterministic validator — works together
before you spend a batch run on it.

---

## 5. Injection & policy guards (eyeball the tool behavior)

The research agents read the open web — the highest injection-exposure surface
in the system. These prove the structural defenses hold. Run as a one-shot
`python -c` (an interactive REPL doesn't play well with some shells):

```bash
uv run python -c "
import logging; logging.basicConfig(level=logging.WARNING)
from jobscout.agents import tools
# soft policy fetches off-list domains but LOGS them loudly:
print(repr(tools.fetch_page('https://example.com')[:100]))
# non-http schemes are refused WITHOUT a request:
print(tools.fetch_page('file:///etc/passwd'))
# hard_whitelist refuses an off-list host outright:
print(tools.fetch_page('https://evil.example', policy=tools.POLICY_HARD))
"
```
- [ ] The off-allowlist soft fetch prints a **loud WARNING** log line
      (`off-allowlist fetch: 'example.com'`) *and* still returns the page text.
- [ ] `file://` and the hard-whitelist off-list host both return a `(fetch
      refused: …)` string — **no network request is made**.

**Why it matters:** capability is the tool list and its policy — nothing more.
An injected "fetch this internal URL" or a `file://` cannot escape these guards.

---

## 6. Dry-run writes nothing (safety)

```bash
uv run jobscout research-company --dry-run --limit 2
```
- [ ] Header says **"(dry-run - nothing written)"**; prints a tally.
- [ ] Confirm nothing persisted:
      ```bash
      sqlite3 data/jobs.db "SELECT COUNT(*) FROM jobs WHERE company_researched_at IS NOT NULL;"
      ```
      Still **0** (assuming you hadn't run it for real yet).

**Why it matters:** `--dry-run` still calls the model (you'll see logs in
`logs/`) but must never write a brief. Same guarantee as every other stage.

---

## 7. Company research — live

```bash
uv run jobscout research-company --limit 3
```
- [ ] Tally shows `considered: 3`, some `briefed`, maybe some `needs_review`,
      and a `had a WTTJ profile` count for the WTTJ offers.
- [ ] Per-offer log lines show `brief stored (medium)` / `brief needs_review`.

Inspect what was stored:
```bash
sqlite3 data/jobs.db "SELECT company, substr(company_brief,1,160) FROM jobs WHERE company_researched_at IS NOT NULL LIMIT 3;"
```
- [ ] `company_brief` is a JSON blob with `summary`/`product`/`culture`/
      `size_signal`/`ai_usage`/`sources`/`confidence`.
- [ ] The claims look **grounded** — tied to the real company, not generic
      filler. (Spot-check one against the company's actual site.)

**Why it matters:** this is the grounded context the scorer will use. If briefs
are vague or wrong, the grounding pass isn't doing its job — read a couple in
full and sanity-check them.

---

## 8. Idempotency (research runs once)

The contract: an already-researched row is **never re-offered** (default mode).
Note ``--limit N`` walks the backlog N at a time, so re-running `--limit 3`
researches the *next* 3 unresearched offers — you only see `considered: 0` once
**all** offers are researched, not after one batch. So test the contract
directly rather than by watching the count:

```bash
uv run python -c "
from jobscout.storage import db
conn = db.connect()
done = {r['id'] for r in conn.execute('SELECT id FROM jobs WHERE company_researched_at IS NOT NULL')}
todo = {r['id'] for r in db.select_jobs_to_research_company(conn)}
print('researched:', len(done), '| default worklist:', len(todo),
      '| overlap (must be 0):', len(done & todo))
"
```
- [ ] **overlap is 0** — no researched row is ever re-offered.

Force a redo (the deliberate override):
```bash
uv run jobscout research-company --redo --limit 1
```
- [ ] It re-researches a row despite the stamp (`--redo` re-offers all).

**Why it matters:** "to research" = passed/needs_review AND not yet stamped. A
re-run only picks up new offers; `--redo` is the override. Same idempotency shape
as every Phase 2 stage. (The same holds for `research-address`: its default
worklist never re-offers an `address_source='agent'` row.)

---

## 9. Address research — the tail + the validation net

First see who the deterministic chain couldn't place (the agent's worklist):
```bash
sqlite3 data/jobs.db "SELECT company, location FROM jobs WHERE filter_status IN ('passed','needs_review') AND (address_source IS NULL OR address_source='needs_address') LIMIT 10;"
```
- [ ] You see some bare-"Paris" / vague-location offers (if any exist in your
      current data). If there are none, the agent simply has nothing to do —
      that's fine.

```bash
uv run jobscout research-address --limit 5
```
- [ ] Tally shows `needed agent: N` (rows the chain couldn't place) and a split
      of `resolved` (agent found a valid IDF address) vs `unresolved` (no valid
      address; centroid fallback stands).

Inspect a resolved one:
```bash
sqlite3 data/jobs.db "SELECT company, address, address_source, address_confidence, commute_minutes FROM jobs WHERE address_source='agent';"
```
- [ ] `address_source` is `agent`, `address` is a real IDF street address,
      confidence is `medium` (never `high` — web-found addresses are capped).
- [ ] **`commute_minutes` is NULL** — the address agent deliberately leaves
      routing to `enrich-commute` (next step).

**Why it matters:** every stored `agent` address passed the geocode + IDF gate.
A hallucinated or prompt-injected address wouldn't geocode into Île-de-France,
so it never reaches this table — it fell back to the centroid instead.

---

## 10. The address → enrich-commute handoff

Now route the freshly-placed addresses (enrich-commute is the sole router):
```bash
uv run jobscout enrich-commute
```
- [ ] The `agent`-sourced rows now get a `commute_minutes`:
      ```bash
      sqlite3 data/jobs.db "SELECT company, address_source, commute_minutes, commute_mode FROM jobs WHERE address_source='agent';"
      ```
      `commute_minutes` is now populated for the agent rows.

**Why it matters:** this proves the ordering works — research places the address,
enrich-commute picks it up (via its `resolve_address` pass) and routes it. No
routing logic is duplicated in the agent.

---

## 11. The brief → scorer handoff

Score (or re-score) with the briefs now present:
```bash
uv run jobscout score --rescore --limit 3
```
- [ ] Offers you researched in step 7 get scored.

Confirm the brief actually informed the score — check the reasoning:
```bash
sqlite3 data/jobs.db "SELECT company, score_total, substr(json_extract(score_json,'$.reasoning'),1,300) FROM jobs WHERE company_researched_at IS NOT NULL AND scored_at IS NOT NULL LIMIT 3;"
```
- [ ] The reasoning references company facts (product, size, AI usage, culture)
      that came from the **brief**, not just the posting text.

**Optional — prove the prompt actually carries the brief block:** dry-run scoring
writes the full prompt to `logs/`. Open the newest scoring log and confirm a
`<<<COMPANY_RESEARCH … COMPANY_RESEARCH>>>` block appears, labeled advisory:
```bash
uv run jobscout score --rescore --dry-run --limit 1
# then open the newest file in logs/ and search for COMPANY_RESEARCH
```
- [ ] The prompt contains the fenced `COMPANY_RESEARCH` block for a researched
      offer, and the system prompt calls it "untrusted, advisory".

**Why it matters:** the brief reaches the scorer as delimiter-wrapped, clearly-
labeled context — an injected instruction inside it has nowhere to go (the scorer
has zero tools), and a needs_review/empty brief is omitted entirely.

---

## 12. Fail-soft (one bad row / a dead SearXNG doesn't sink the run)

Simulate SearXNG being unreachable mid-batch:
```bash
# Git Bash:
SEARXNG_URL=http://192.168.1.63:9999 uv run jobscout research-company --limit 2
```
```powershell
# PowerShell:
$env:SEARXNG_URL="http://192.168.1.63:9999"; uv run jobscout research-company --limit 2; Remove-Item Env:\SEARXNG_URL
```
- [ ] The run does **not** crash. `web_search` returns nothing, the agent works
      from what it has (or produces a `needs_review` brief), and the batch
      completes. Rows are still stamped so they aren't retried every run.

**Why it matters:** a search outage, a dead link, or a model hiccup on one offer
must never abort the whole batch — every failure is fail-soft to `needs_review`.

---

## 13. Security & privacy invariants (eyeball checks)

- [ ] **No write tools / no profile access in the research agents.** The only
      tools they get are `web_search` + `fetch_page`:
      ```bash
      grep -n "save_draft\|read_profile\|\.write(\|open(" src/jobscout/agents/address_agent.py src/jobscout/agents/company_agent.py || echo "none — correct"
      ```
      Should print "none — correct" (no write/profile capability in the agents).
- [ ] **Home coordinates never touched by research.** The agents route no
      commute and never read the owner's home location or preferences. Grep for
      the actual home/prefs accessors (not the bare words `home`/`lat`/`lon`,
      which appear benignly in docstrings and in the *office* address handling):
      ```bash
      grep -rn "home_location\|home\.lat\|home\.lon\|config.home\|preferences" \
        src/jobscout/agents/ src/jobscout/pipeline/research_address.py \
        src/jobscout/pipeline/research_company.py || echo "none — correct"
      ```
      Should print "none — correct" (research never reads home/prefs). The only
      `lat`/`lon` in the agents are the *office* address being resolved — the
      destination, never the owner's origin.
- [ ] **`SEARXNG_URL` is only a LAN URL, no secret leaked.** Skim your `.env`
      diff / commit — no API keys or addresses in the agent code paths.
- [ ] **Untrusted-data framing is present.** Confirm fetched text is fenced and
      labeled as data, not instructions, in the prompts:
      ```bash
      grep -rn --include="*.py" "not instructions\|OBSERVATION\|COMPANY_RESEARCH\|DATA TO ANALYZE" src/jobscout/agents/ src/jobscout/pipeline/scoring.py
      ```
      Should show the delimiter/labeling in the loop, both agents, and the scorer
      (the `--include="*.py"` keeps compiled `.pyc` files out of the results).
- [ ] **Still no email-send capability** (the standing invariant):
      ```bash
      grep -ri "smtplib\|sendmail\|SMTP" src/ || echo "none — correct"
      ```

---

## 14. Full pipeline dry pass (the intended daily order)

With a real day's ingest, the intended order end-to-end:
```bash
uv run jobscout ingest
uv run jobscout filter
uv run jobscout enrich-linkedin           # backfill LinkedIn descriptions
uv run jobscout research-address          # place the unroutable tail
uv run jobscout research-company          # grounded company briefs
uv run jobscout enrich-commute            # route everything (incl. agent addresses)
uv run jobscout score                     # score with briefs as context
```
- [ ] Each stage's tally is sane and the counts flow (what `filter` passed is
      what later stages consider).
- [ ] No stage crashes; failures are counted, not fatal.

**Why it matters:** this is the real activation sequence. Research runs *before*
enrich-commute and score, so fresh offers get a sharpened address + a company
brief before they're routed and scored.

---

## Sign-off

- [ ] Steps 1–14 all pass.
- [ ] You confirmed a stored `agent` address is a real IDF street address (step 9)
      and got its commute from `enrich-commute` (step 10).
- [ ] You read a couple of company briefs and they're **grounded**, not generic
      (step 7), and one showed up in a score's reasoning (step 11).
- [ ] The injection/policy guards and the security invariants held (steps 5, 13).

If all boxes are checked, the Phase 3 research agents are verified. What remains
of Phase 3 is the cover-letter agent, which reuses this same tool loop with a
stricter `hard_whitelist` fetch policy and one sandboxed `save_draft`.

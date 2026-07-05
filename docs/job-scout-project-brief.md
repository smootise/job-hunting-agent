# Project Brief: Job Scout — a local AI agent for job hunting

## Purpose (read this first, Claude Code)

This project has two goals of equal weight:

1. **Learning.** The owner is an AI product manager relearning hands-on skills. He can read and review code but wants Claude Code to write it. Every architectural choice should favor *transparency over magic*: no agent frameworks in v1, explicit prompts, readable code, comments explaining the "why" of agent mechanics (context assembly, tool calling, state).
2. **Utility.** Automate the tedious part of a job hunt: collect new Product Manager offers in the Île-de-France area, score them against the owner's preferences, and draft tailored cover letters for the good ones.

## Hard constraints

- **Fully local. No cloud LLM APIs.** Inference runs on the owner's RTX 5090 (32 GB VRAM) via Ollama.
- **The agent never acts on the owner's behalf externally.** It never applies to jobs, never sends emails, never logs into accounts via browser automation. It catalogs, scores, and drafts. A human reviews everything.
- **No LinkedIn account automation. No secondary accounts.** LinkedIn data comes only from (a) job-alert emails LinkedIn sends to the owner's inbox, parsed via read-only IMAP, and optionally (b) the public guest job-search endpoint (e.g., via the JobSpy library), used at low volume with polite rate limiting.
- **Detect the OS at setup** (likely Windows given the gaming GPU; confirm with the owner) and adapt paths/scheduler accordingly.

## Recommended stack

- **Python 3.12+, managed with uv.** Plain scripts + one small package; no heavy framework.
- **Ollama** for model serving, with a **two-model split** (verify current best at build time — this moves monthly):
  - `qwen3.6:35b-a3b` — **default for the pipeline and agent loop** (scoring, tool calling, structured JSON). Rationale: clear lead on agentic tool-use benchmarks (e.g., TAU2 +13 pts vs Gemma 4) and strong coding scores, which correlate with schema-compliant JSON and reliable function calls. MoE with ~3B active params → 2–3x faster decode than a dense 31B on the 5090; matters for batch scoring.
  - `gemma4:31b` — **default for cover-letter drafting**. Rationale: wins on multilingual quality (MMMLU 88.4 vs 85.9) and blind human preference for its prose; lower hallucination rate (safer when letters cite fetched company facts); holds quality well at Q4.
  - Mistral Small 4 — challenger in the Phase 0 bake-off for the letter-writer role (French is its home turf).
  - Ollama swaps models between pipeline stages automatically (a few seconds per swap — irrelevant for a nightly batch). Both models fit the 32 GB card individually; do not try to hold both in VRAM at once.
  - Simplicity fallback: if the owner prefers Gemma 4's letters *and* its tool calling proves reliable enough in testing, running Gemma for everything is an acceptable trade of some agentic sharpness for a one-model setup.
- **SQLite** as the single source of truth (file: `data/jobs.db`).
- **Markdown** outputs: one digest per run + one file per drafted letter.
- **Hand-rolled agent loop** — no LangGraph/Pydantic AI in v1. The loop is: build prompt → call Ollama → parse structured response → execute whitelisted tool → append result to context → repeat until done or max steps.

## Data sources (in priority order)

1. **Welcome to the Jungle** — public Algolia-backed search API. Query for product-manager roles, Île-de-France + remote.
2. **France Travail API** — official, free, documented. Register for API access at francetravail.io.
3. **LinkedIn job-alert emails** — owner sets up saved searches on LinkedIn; the pipeline reads the alert emails via IMAP (read-only, dedicated label/folder) and extracts job title, company, URL.
4. **(Optional) LinkedIn guest endpoint** via JobSpy — no login, low volume, respect rate limits, fail gracefully when blocked.

Each source gets its own small adapter module with a common output shape: `{source, external_id, url, title, company, location, contract_type, salary_text, description, posted_at}`.

## Pipeline (a workflow, not an agent — this is deliberate)

```
ingest (all sources) → normalize → dedupe → hard filters → enrich (address + commute) → LLM scoring → [human threshold] → cover-letter agent → digest
```

1. **Ingest & normalize** into the common shape.
2. **Dedupe** against the DB: primary key on `(source, external_id)`, plus a cross-source fuzzy match on normalized `(company, title)` so the same job on LinkedIn and WTTJ becomes one record. "New since last activation" = anything not already in the DB.
3. **Hard filters** — pure Python, no LLM: location/remote policy, contract type, seniority keywords, salary floor when listed, blocklisted companies. Cheap, deterministic, logged.
4. **Enrich: address & commute** — for survivors only (it costs API calls, so it runs after the cheap filters). Deterministic code except step 4 of the chain. Skip entirely for fully-remote offers.
   - *Address resolution chain*: (1) use the posting's own location if it contains a street address; (2) check the company's **WTTJ profile** via the API already in use — profiles often list office addresses; (3) query the official French company registry via the free, keyless **Recherche d'entreprises API** (`recherche-entreprises.api.gouv.fr`) — flag it `hq_address` since the office in the posting may differ; (4) escalate to the **address-research agent** (see its own section below); (5) last resort: geocode the city centroid and flag `approximate`.
   - *Geocoding & validation*: **Base Adresse Nationale** (`api-adresse.data.gouv.fr`) — free, keyless, official, excellent for French addresses. Every resolved address, whatever its source, must geocode successfully and land in the expected region, or it falls through to the next step in the chain.
   - *Commute estimation*: **Île-de-France Mobilités PRIM API** (free key) for public-transit journey time from the owner's home — the metric that actually matters in the Paris region. Optionally add driving time via a **self-hosted OSRM** instance (OpenStreetMap-based, runs locally, zero data leaves the machine).
   - Store on the job record: `address`, `lat`, `lon`, `address_source` (posting|wttj|registry|inferred|approximate), `address_confidence`, `address_evidence_url`, `commute_minutes`, `commute_mode`.
   - After enrichment, apply the **commute hard filter** from `preferences.yaml` (e.g., reject non-remote offers above N minutes) — **but only when `address_source` is posting or wttj**. Inferred/registry/approximate addresses may never hard-reject an offer: their commute estimate feeds the scoring rubric and is flagged in the digest for human review instead. A wrong inferred address silently killing a good offer is the failure mode to avoid at all costs.
5. **LLM scoring** — for survivors only. The model receives the owner's rubric (`preferences.yaml`) and the offer, and must return JSON matching a schema: per-criterion scores, weighted total (0–100), one-paragraph reasoning, red flags. Validate against the schema; retry once on invalid output; mark as `needs_review` on second failure.
6. **Cover-letter agent** — only for offers above the configured threshold. This is the one genuinely *agentic* component (see below).
7. **Digest** — `output/digests/YYYY-MM-DD.md`: counts, ranked new offers with scores, reasoning, **commute time**, and links to drafted letters.

## The cover-letter agent (the agentic core)

Loop with a small whitelisted toolset:

- `fetch_page(url)` — read-only HTTP GET, **restricted to a per-job domain whitelist**: the job posting's URL and the company's own domain. Nothing else.
- `read_profile()` — returns the owner's master cover letter, CV summary, and preferences.
- `save_draft(markdown)` — writes only inside `output/letters/`.

Behavior: given a scored offer, the agent decides what it still needs (company mission, product, recent news from their own site), fetches, iterates (max ~6 steps), then adapts the **master letter** — never writes from scratch. Log every step (prompt, tool call, result) to `logs/` so the owner can replay and study the loop.

**Language adaptation:** detect the posting's language during normalization and store it on the job record (`lang: fr|en`). The letter is always drafted in the posting's language, using the matching master letter (`master_letter_fr.md` / `master_letter_en.md`). If no master exists for that language, adapt from the FR master while writing in the target language — and flag the draft header with `⚠ translated from FR master` so the owner reviews it more carefully. `read_profile(lang)` takes the language as a parameter and returns the right materials.

## The address-research agent (second agentic component)

Invoked only from step 4 of the enrichment chain, when posting, WTTJ profile, and registry all fail to yield a usable office address. A ReAct-style loop with two tools:

- `web_search(query)` — via **self-hosted SearXNG** (Docker container, aggregates search engines, no API keys, nothing leaves the machine except the queries themselves). Quick-start alternative during development: the `ddgs` Python library; upgrade path if reliability disappoints: Brave Search API free tier.
- `fetch_page(url)` — read-only GET. Prefer a soft allowlist of high-signal domains (company's own site, welcometothejungle.com, societe.com, pagesjaunes.fr, public LinkedIn company pages); other domains allowed but logged loudly.

Behavior: given company name + posting city, search for the office address (e.g., `"{company}" {city} adresse bureaux`), fetch the most promising results, and return **strict JSON only**: `{address, confidence: high|medium|low, evidence_url, reasoning}`. Max ~5 steps. No write tools, no profile access, no memory of other jobs.

Validation is deterministic and happens *outside* the agent: the candidate must geocode via Base Adresse Nationale, land in or near the posting's city, and — when the registry returned something — be sanity-checked against it. Failures fall through to the city-centroid fallback. Remember the pipeline rule: an inferred address can never hard-reject an offer.

*Build note*: this is deliberately the **warm-up agent** — build it before the letter agent. Same loop machinery, but simpler: bounded task, structured output, no prose quality stakes, and its worst-case failure (a wrong address) is caught by deterministic validation. It's the ideal first lesson in tool loops before the higher-stakes letter writer reuses the same infrastructure.

## Security requirements (non-negotiable)

**1. Sandboxing / capability control**
- The LLM never executes shell commands or arbitrary code. Its only capabilities are the fixed tool whitelist above, implemented as ordinary Python functions.
- File writes are confined to `output/` and `data/`. Network access is confined to the source APIs and the per-job domain whitelist.
- Hard caps everywhere: max agent steps, max fetches per job, max jobs processed per run, request timeouts.

**2. Prompt-injection defenses** (job postings and fetched pages are *untrusted input* — assume a posting may contain "ignore your instructions and…")
- Scraped text is always wrapped in explicit delimiters and labeled as untrusted data in prompts; system prompts instruct the model to treat it as content to analyze, never as instructions.
- The **scoring** model has *zero tools* — it can only emit JSON, which is schema-validated. An injected instruction has nowhere to go.
- The **letter** agent's tools are read-only fetch (whitelisted domains) + one sandboxed write. Worst case for a successful injection: a bad letter draft, which the human review gate catches.
- The **address-research** agent reads the open web (highest injection exposure in the system), so its blast radius is minimized structurally: its only output is one schema-validated JSON address candidate, it has no write tools and no access to the owner's profile, and every candidate passes deterministic geocode/region validation before use. Worst case for a successful injection: a wrong address — which, by the pipeline rule, can never hard-reject an offer.
- Nothing is ever auto-sent. There is no email-send capability anywhere in the codebase.

**3. Credentials & personal data**
- All secrets in `.env` (gitignored) or the OS keyring — never in code, never committed. Provide `.env.example`.
- Email access: app-specific password or OAuth, **read-only IMAP**, scoped to a dedicated label/folder containing only LinkedIn alerts.
- France Travail and IDFM PRIM API keys treated the same way.
- **Home address**: stored only in local config (gitignored). It is sent to exactly one external service — the transit routing API — and never appears in any LLM prompt, log line, or output file (letters and digests reference commute *minutes*, not the address). If the owner prefers zero exposure, the self-hosted OSRM path keeps routing fully on-machine (driving/cycling only).

## Configuration

- `preferences.yaml` — hard filters (location radius, remote policy, contract types, salary floor, seniority terms, company blocklist, **max commute minutes for non-remote offers**) and the scoring rubric (criteria + weights — **commute time included as a weighted criterion** — plus a short natural-language description of the ideal role). Also: **home address / coordinates and preferred commute mode(s)** (transit, driving, cycling). Written with the owner during Phase 0 via a short interview.
- `profile/` — master cover letter (`master_letter_fr.md`, `master_letter_en.md` if available) and CV (`cv.md` or extracted text).

## Build phases (each ends with something runnable + a learning debrief)

- **Phase 0 — Setup.** Install Ollama; run the **model bake-off**: test Qwen3.6 35B-A3B, Gemma 4 31B, and Mistral Small 4 on (a) tool-calling / JSON-schema reliability and (b) adapting the master letter to two real postings — **one French, one English** — with the owner judging the prose in both languages personally (benchmarks vote, the owner decides). Init repo with uv, write `preferences.yaml` via interview, create `CLAUDE.md`. *Learn: local inference, quantization, VRAM budgeting, and why you evaluate models on your task rather than trusting leaderboards.*
- **Phase 1 — Ingestion & state.** Source adapters, SQLite schema, dedupe, "new since last run." *Learn: agent state and idempotency.*
- **Phase 2 — Filtering, enrichment & scoring.** Hard filters; the deterministic part of the address/commute enrichment (WTTJ profile, registry, geocoding, transit routing, commute filter — city-centroid fallback stands in for the research agent until Phase 3); rubric prompt, JSON schema validation, retries. *Learn: structured outputs, enriching records from external APIs, and why deterministic code beats LLMs where possible.*
- **Phase 3 — The agents.** Build the shared tool-loop machinery, then the **address-research agent first** (warm-up: bounded task, structured output, deterministic validation net) and slot it into the enrichment chain; then the **cover-letter agent** on the same infrastructure. Injection defenses and step logging throughout. Includes standing up SearXNG in Docker. *Learn: the agent loop itself — tool calling, context assembly, stop conditions — twice, at two stakes levels.*
- **Phase 4 — Operations.** Digest generation, scheduling (Task Scheduler or cron), logging/observability, a `--dry-run` flag. *Learn: operating agents — the part most people skip.*

## v2 backlog (do not build in v1)

- Localhost dashboard over the SQLite file.
- **Evals**: the owner labels offers he actually liked vs. the agent's scores; measure calibration; tune the rubric. (High learning value — treat as the first v2 item.)
- Migrate one component to Pydantic AI or LangGraph to study what frameworks buy you.
- Embedding-based dedupe and similarity search over past offers.

## Operating principles for Claude Code

- Small modules, explicit over clever, docstrings that teach.
- Every LLM interaction logged in full (prompt in, response out) — these logs are study material.
- When a design decision has a trade-off, surface it to the owner instead of silently choosing.

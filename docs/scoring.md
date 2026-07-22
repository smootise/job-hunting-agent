# LLM scoring (as-built)

The Phase 2 stage that turns a filtered, enriched offer into a ranked score. It
runs after the hard filters + commute enrichment and before the digest, on
`passed` + `needs_review` offers only (the scorer owns the needs_review tail;
rejected offers are never scored). Each offer gets one zero-tool LLM call, a
schema-validated result, and a weighted 0–100 total.

CLI: `jobscout score [--limit N] [--model NAME] [--rescore] [--commute-only] [--ids ID ...] [--dry-run]`.

Code: `pipeline/scoring.py` (pure: prompt, validation, weighted total),
`pipeline/commute_score.py` (pure: the commute sub-score curve),
`pipeline/score_stage.py` (orchestration, mirroring `enrich_commute.py`).

## What the LLM does — and does not — do

The model (`qwen3.6:35b-a3b`, zero tools) scores the **10 qualitative rubric
criteria** (0–10 each) and infers two things from the posting:

- `onsite_days` (0–5) — days per week required on site. When the posting states
  a cadence, it uses it; when unstated, it infers from the remote policy:
  **fully remote → 0, hybrid/unstated → 3** (the upper end of the ideal 2/3, so
  a hidden long commute can't inflate the score), **fully onsite → 5** — and
  notes the assumption in its reasoning. This is also where the scorer resolves
  the ~33 France Travail offers whose remote policy is stated *nowhere*
  structured (the FT API has no remote field) — see `docs/architecture.md`.
- `remote_policy` — its read of `remote|hybrid|onsite|unknown`.

It returns strict JSON: `criteria_scores`, `onsite_days`, `remote_policy`, a
one-paragraph `reasoning`, and `red_flags[]`. **`weekly_commute_fit` is NOT in
the LLM's schema** — it's Python-owned (below).

The prompt lists only the qualitative criteria and their rubric descriptions,
plus the owner's `ideal_role_description`. On an invalid-JSON response the stage
**retries once**, then marks the offer `score_status = needs_review` (no bogus
score). Every call is logged in full to `logs/` by `llm/client.py`.

## `weekly_commute_fit` — computed in Python, blended afterward

Owner's decision (`docs/architecture.md` → "Current state & next"): the commute
criterion is a deterministic function of the enrichment stage's one-way
`commute_minutes` and the LLM's `onsite_days`, **not** an LLM judgment — so a
better address or a changed onsite estimate re-scores for free
(`jobscout score --rescore`) with no model call.

### The curve (`commute_score.py`) — owner-calibrated

`weekly_commute_fit` (0–10) =
`clamp( weekly_component(weekly_total) − oneway_penalty(one_way), 0, 10 )`
where `weekly_total = one_way × 2 × onsite_days`.

Two inputs, not just the brief's weekly total, because the owner's rankings show
a long **single leg** hurts beyond what weekly total alone explains
(90 min × 2 days = 360/wk scored **2**, but 40 min × 5 days = 400/wk scored
**5** — more weekly, higher score). And "many short trips" beats "fewer long
ones" at a similar total (25 min × 5 days scored **9** vs 50 min × 3 days
scored **5**).

- `weekly_component` piecewise-linear anchors (weekly min → score):
  `0→10, 120→10, 200→8.8, 300→7.0, 400→5.6, 480→4.0, 600→1.6, 720→0.2, 800→0`.
- `oneway_penalty(one_way)`: `0` up to 25 min; grows linearly to ~2.6 by 75 min;
  steepens beyond (each further 15 min ≈ −1.6) — the rubric's "differences above
  75 min one-way matter much more than 20 vs 40".

Calibrated to the owner's 12 ranked scenarios (mean abs error ≈ 0.39). A 1-point
wobble on the weight-9 criterion is ≈0.1 on the final 100.
`tests/test_commute_score.py` pins those 12 rankings within tolerance, so a
future edit to an anchor is a conscious, visible change — not a silent recalibration.

### The owner's 12 calibration scenarios

| one-way | onsite/wk | weekly | owner score |
|--:|--:|--:|--:|
| 20 | 3 | 120 | 10 |
| 30 | 2 | 120 | 10 |
| 25 | 5 | 250 | 9 |
| 40 | 3 | 240 | 7 |
| 60 | 2 | 240 | 6 |
| 40 | 5 | 400 | 5 |
| 50 | 3 | 300 | 5 |
| 60 | 4 | 480 | 3 |
| 75 | 3 | 450 | 3 |
| 90 | 2 | 360 | 2 |
| 90 | 4 | 720 | 0 |
| 110 | 3 | 660 | 0 |

## The weighted total (`compute_total`)

Rubric weights are **relative** (they sum to **75**, not 100), so we normalize:

```
total = 100 × Σ(weight × sub_score/10) / Σ(weight)
```

over the criteria included. The commute criterion is included with its Python
sub-score when the commute is known.

### Unknown commute → dropped, never zeroed

When `commute_minutes` is NULL — a `needs_address` row (bare "Paris"), a routing
failure, or an offer not yet enriched — `weekly_commute_fit` is **dropped from
both the numerator and the denominator** (the total is renormalized over the
remaining 10 criteria) and a `"commute unknown — pending address resolution"`
red_flag is added. A NULL is *unknown*, not *0 minutes*: injecting a zero (or a
max) would silently corrupt the score. These are exactly the rows the Phase 3
address agent will sharpen, after which the commute is folded in — via
`--commute-only` (recompute the Python sub-score from the stored breakdown, no
LLM call — the cheap, preferred path) or a full `--rescore`.

A fully-remote offer is enriched with `commute_minutes = 0` and correctly scores
the maximum on this criterion (it is *not* the unknown case).

## Other red_flags the pipeline adds

- **"salary not stated"** when the offer has no `salary_text` (rubric's
  `compensation_attractiveness` says don't tank it, but flag for clarification).
- The **"assumed CDI"** filter note (from `assume_cdi_when_unstated`) is surfaced
  in the prompt so the model treats the contract as uncertain, not a stated fact.

## Stored columns (on `jobs`)

`score_total` (REAL, headline weighted 0–100, for sorting the digest),
`score_status` (`scored` | `needs_review`), `score_json` (the full breakdown:
per-criterion scores, the commute sub-score + whether it was included,
`onsite_days`, `remote_policy`, reasoning, red_flags), `scored_at` (idempotency
stamp — a re-run skips scored rows; `--rescore` redoes all). Added additively via
`_ADDED_COLUMNS`, so an existing DB upgrades in place.

## Idempotency & scope

`select_jobs_to_score` picks `filter_status IN ('passed','needs_review')` with
`scored_at IS NULL` (unless `--rescore`). Scoring is **independent of
enrichment**: a row with a NULL commute is still scored (commute flagged
unknown), so there's no ordering dependency on `enrich-commute`; `--rescore`
picks up commute improvements later.

### Targeted & commute-only runs

Two flags scope the same stage without a full re-score of everything:

- **`--ids ID ...`** restricts the run to specific job ids (still gated on the
  eligibility predicate — a rejected/unfiltered id is silently excluded, never
  scored). An explicit id list means *"score these"*, so it overrides the
  `scored_at IS NULL` gate. This is the targeted-run primitive the webapp drives;
  `run_score(ids=[...])` is the programmatic entry point.
- **`--commute-only`** recomputes **only** `weekly_commute_fit` + the blended
  total from each row's *existing* `score_json` — **no LLM call**. The
  qualitative criteria, `onsite_days`, `remote_policy` and `reasoning` are read
  back verbatim; only the Python-owned commute sub-score (from a possibly-changed
  `commute_minutes`) and the total change, and a now-resolved commute drops its
  stale `"commute unknown"` red_flag. Rows with no prior score are skipped
  (nothing to fold a commute into). **Why not just `--rescore`?** The model never
  sees the commute (security invariant below), so re-running it after a commute
  change adds only nondeterministic noise to the qualitative scores — the
  commute-only recompute is both cheaper *and* more correct. Composes with
  `--ids` (`score --commute-only --ids 61 75`).

## Security invariants (held, and tested)

- **Zero tools** for the scoring model — one `generate` call, no shell / network
  / writes.
- The untrusted posting is wrapped in `<<<JOB_POSTING … JOB_POSTING>>>`
  delimiters and labeled *data to analyze, never instructions*; the system
  prompt tells the model to ignore any embedded commands.
- **No home location and no commute data of any kind enter the prompt, the logs,
  or `score_json`** — commute is Python-owned and the owner's location never
  reaches an LLM. `tests/test_scoring.py::test_prompt_has_no_home_location_or_commute`
  asserts this.
- File writes confined to `data/` (DB) and `logs/` (the mandatory call logs).

## Prerequisites (owner, out-of-band)

- **Ollama running** with `qwen3.6:35b-a3b` pulled (`ollama pull qwen3.6:35b-a3b`).
  Only one model resides in VRAM at a time on the 32 GB card; Ollama swaps
  automatically (a few seconds per swap — fine for a batch job).
- Offers already through `jobscout filter` (and ideally `enrich-commute`, though
  scoring doesn't require it).

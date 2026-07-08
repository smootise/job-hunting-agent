# Phase 0 model bake-off

Helps you pick the pipeline model and the letter-drafting model, per the
project brief's Phase 0 ("benchmarks vote, the owner decides"). Standalone
tooling — not part of the `jobscout` package, not wired into the real
pipeline.

Candidates (edit `CANDIDATE_MODELS` in `_common.py` if this changes):
`qwen3.6:35b-a3b`, `gemma4:31b`, `mistral-small3.2:latest` (the closest
Ollama tag to the brief's "Mistral Small 4" at build time — re-check for
a newer tag before relying on this).

Test data: the two real posting + sent-letter pairs in `samples/`
(NEXTON, French; Dataiku, English) — these are jobs the owner actually
applied to, so the sent letter is a genuine quality bar, not a fixture.

## 1. JSON / schema reliability

```
uv run python scripts/bakeoff/tool_json_reliability.py
```

Sends each model a scoring prompt shaped like the real Phase 2 scoring
step (see CLAUDE.md's `scoring_rubric` section) against both postings,
with one retry on invalid output — same policy as the real pipeline.
Writes `output/bakeoff/tool_json_reliability_scorecard.md`.

Deliberately strict: the harness does not strip markdown code fences
before parsing. A model that wraps its JSON in \`\`\`json...\`\`\` despite
being told not to will show up as "Valid JSON: False" — that is a real
reliability signal (the real pipeline would hit the same failure), not a
harness bug.

## 2. Letter-adaptation prose quality

Requires master letters first — see `profile/README.md` for how to seed
`profile/master_letter_fr.md` and `profile/master_letter_en.md` from the
`samples/` letters (strip the posting-specific sentences, keep the rest).

```
uv run python scripts/bakeoff/letter_adaptation.py
```

For each model, adapts the matching-language master to each posting and
writes `output/bakeoff/<model>_<company>_<lang>.md`, alongside
`output/bakeoff/REFERENCE_<company>_<lang>_sent.md` (the letter actually
sent). Read them side by side and judge personally — no automated judge
is used here on purpose.

## Verdict (2026-07-08, run against the NEXTON/FR and Dataiku/EN samples)

| Model | JSON reliability | FR prose vs. reference | EN prose vs. reference | Notes |
|---|---|---|---|---|
| qwen3.6:35b-a3b | 100% valid, 1st try, both postings | Best — idiomatic, integrates posting-specific vocabulary (Discovery/Delivery/Run, "grands comptes") into existing sentences | Best — mirrors the posting's own framing (e.g. "not diving into production code") without inventing facts | Richest, most posting-grounded reasoning on scoring too (e.g. caught an ARR/incentive tension in the Dataiku posting that the others missed) |
| gemma4:31b | 100% valid, 1st try, both postings | Good but thinner — fluent, correctly tailored, lost some of the reference's specificity | Good but more generic/paraphrased than qwen | Solid, safe, but consistently less tailored than qwen on both letters |
| mistral-small3.2:latest | Failed both — wrapped JSON in markdown fences despite instructions, invalid even after 1 retry | Weakest — padded to 5 paragraphs with redundant content not in the master | Weakest — kept the original 3 paragraphs near-verbatim then bolted on 3 generic ones (7 total vs. 4) | Ruled out for both roles |

**Chosen pipeline model:** `qwen3.6:35b-a3b`
**Chosen letter model:** `qwen3.6:35b-a3b`

This overturns the brief's original expectation that Gemma 4 would win on
letter prose — on this data qwen won both roles outright. `gemma4:31b`
and `mistral-small3.2:latest` are dropped; CLAUDE.md's Models section has
been updated to a single-model setup. If future postings surface cases
where qwen underperforms, re-run this bake-off before switching back.

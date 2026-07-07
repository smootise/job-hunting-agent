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

## Your verdict

Fill in after reading the outputs:

| Model | JSON reliability | FR prose vs. reference | EN prose vs. reference | Notes |
|---|---|---|---|---|
| qwen3.6:35b-a3b | | | | |
| gemma4:31b | | | | |
| mistral-small3.2:latest | | | | |

**Chosen pipeline model:** _______
**Chosen letter model:** _______

If either differs from CLAUDE.md's current defaults (`qwen3.6:35b-a3b`
pipeline / `gemma4:31b` letters), update that file's Models section.

"""Bake-off harness #1: strict-JSON / schema-compliance reliability.

Sends each candidate model a scoring prompt shaped exactly like the real
Phase 2 scoring step will be (see CLAUDE.md's scoring_rubric section:
per-criterion scores, weighted total, one-paragraph reasoning,
red_flags), against the two real postings in samples/, and measures:

  - valid JSON rate (did it parse at all?)
  - schema-conformance rate (right keys, right types?)
  - retries needed to reach a valid/conformant response

This is the harness's proxy for "agentic tool-use reliability" per the
brief's Phase 0 bake-off criteria — real tool-calling comes later in
Phase 3, but schema-compliant JSON is the same underlying skill.

Usage: uv run python scripts/bakeoff/tool_json_reliability.py
"""

from __future__ import annotations

import json

from _common import CANDIDATE_MODELS, OUTPUT_DIR, load_samples, wrap_untrusted

from jobscout.llm.client import generate

SCHEMA_KEYS = {"criteria", "weighted_total", "reasoning", "red_flags"}

SYSTEM_PROMPT = """You are a job-offer scoring assistant. You have no tools \
and no ability to take any action other than emitting JSON. Score the \
offer below on a 0-10 scale for each criterion, then output STRICT JSON \
matching this shape and nothing else (no markdown fences, no commentary):

{
  "criteria": {"culture_fit": 0, "role_scope": 0, "compensation": 0},
  "weighted_total": 0,
  "reasoning": "one paragraph",
  "red_flags": []
}"""

MAX_RETRIES = 1


def score_once(model: str, posting_block: str) -> tuple[str, bool, bool]:
    """Return (raw_response, is_valid_json, is_schema_conformant)."""
    result = generate(model=model, prompt=posting_block, system=SYSTEM_PROMPT)
    raw = result.response.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw, False, False
    conformant = SCHEMA_KEYS.issubset(parsed.keys())
    return raw, True, conformant


def evaluate_model(model: str, posting_block: str) -> dict:
    attempts = 0
    valid = conformant = False
    raw = ""
    while attempts <= MAX_RETRIES:
        raw, valid, conformant = score_once(model, posting_block)
        attempts += 1
        if valid and conformant:
            break
    return {
        "attempts": attempts,
        "valid_json": valid,
        "schema_conformant": conformant,
        "raw_response": raw,
    }


def main() -> None:
    samples = load_samples()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    for model in CANDIDATE_MODELS:
        for sample in samples:
            posting_block = wrap_untrusted("job_posting", sample.posting)
            print(f"Scoring {sample.company} ({sample.lang}) with {model}...")
            outcome = evaluate_model(model, posting_block)
            rows.append({"model": model, "company": sample.company, **outcome})

    scorecard_path = OUTPUT_DIR / "tool_json_reliability_scorecard.md"
    lines = [
        "# Bake-off: JSON / schema reliability\n",
        "| Model | Posting | Valid JSON | Schema-conformant | Attempts |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['model']} | {row['company']} | {row['valid_json']} | "
            f"{row['schema_conformant']} | {row['attempts']} |"
        )
    scorecard_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nScorecard written to {scorecard_path}")


if __name__ == "__main__":
    main()

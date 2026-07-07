"""Bake-off harness #2: letter-drafting prose quality.

For each candidate model and each real posting in samples/, adapt the
matching-language master letter (profile/master_letter_fr.md /
_en.md) to that posting, and write the draft to output/bakeoff/ next to
the owner's real sent letter. The owner reads both and judges the prose
personally — the brief is explicit that benchmarks vote, the owner
decides. There is deliberately no automated judge here.

Requires profile/master_letter_fr.md and profile/master_letter_en.md to
exist (see profile/README.md for how to seed them from samples/).

Usage: uv run python scripts/bakeoff/letter_adaptation.py
"""

from __future__ import annotations

from _common import CANDIDATE_MODELS, OUTPUT_DIR, REPO_ROOT, load_samples, wrap_untrusted

from jobscout.llm.client import generate

PROFILE_DIR = REPO_ROOT / "profile"

SYSTEM_PROMPT = """You are helping a Product Manager adapt their master \
cover letter to a specific job posting. You have no tools. Adapt the \
master letter below to the posting: keep the owner's real background, \
voice, and structure, but tailor the specific points raised to what the \
posting emphasizes. Do not invent facts about the owner that are not in \
the master letter. Write only the letter body, in the same language as \
the posting, with no preamble or commentary."""


def build_prompt(master_letter: str, posting: str) -> str:
    return (
        f"MASTER LETTER (the owner's own writing — adapt, don't replace):\n"
        f"{master_letter}\n\n"
        f"{wrap_untrusted('job_posting', posting)}"
    )


def main() -> None:
    samples = load_samples()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    masters: dict[str, str] = {}
    for lang, filename in (("fr", "master_letter_fr.md"), ("en", "master_letter_en.md")):
        path = PROFILE_DIR / filename
        if path.exists():
            masters[lang] = path.read_text(encoding="utf-8")
        else:
            print(f"Warning: {path} not found — skipping {lang} drafts. See profile/README.md.")

    for model in CANDIDATE_MODELS:
        for sample in samples:
            master = masters.get(sample.lang)
            if master is None:
                continue
            print(f"Drafting {sample.company} ({sample.lang}) with {model}...")
            prompt = build_prompt(master, sample.posting)
            result = generate(model=model, prompt=prompt, system=SYSTEM_PROMPT)

            safe_model = model.replace(":", "-").replace("/", "-")
            draft_path = OUTPUT_DIR / f"{safe_model}_{sample.company}_{sample.lang}.md"
            draft_path.write_text(result.response.strip(), encoding="utf-8")

            reference_path = OUTPUT_DIR / f"REFERENCE_{sample.company}_{sample.lang}_sent.md"
            if not reference_path.exists():
                reference_path.write_text(sample.sent_letter, encoding="utf-8")

    print(f"\nDrafts written to {OUTPUT_DIR} (REFERENCE_*.md = the letter actually sent).")


if __name__ == "__main__":
    main()

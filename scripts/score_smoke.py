"""Fail-fast smoke test for the LLM scoring wiring, across all three sources.

Run this after the DB has some filtered offers, to confirm the scoring stage
handles each source's quirks before (or instead of) a full `jobscout score`
run. The three sources stress different prompt paths:

  * **wttj** — rich descriptions, contract usually stated.
  * **france_travail** — often no salary, contract sometimes unstated (the
    "assumed CDI" note), and NO structured remote field (the scorer must infer
    the remote policy of these silent offers).
  * **linkedin_email** — description present only if `enrich-linkedin` backfilled
    it; some carry no commute (the unknown-commute drop + red_flag path).

It picks one scoreable (`passed`/`needs_review`) offer per source — preferring
an un-scored one, else any — runs the real model once each via the same
`jobscout.pipeline.scoring` + `commute_score` code the pipeline uses, and prints
the result. **Read-only: it never writes to the DB** (mirrors `jobscout score
--dry-run`), so it's safe to re-run even after a full scoring pass. A green run
means the prompt, the model's JSON, validation, and the Python commute blend all
work end-to-end for every source. The full LLM-call logs still land in `logs/`.

Usage:  uv run python scripts/score_smoke.py

Privacy note: like the pipeline, the prompt sent to the model contains no home
location and no commute data — commute is Python-owned; the model sees only the
public posting and the rubric.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from jobscout import config  # noqa: E402
from jobscout.llm import client as llm_client  # noqa: E402
from jobscout.models import JobRecord  # noqa: E402
from jobscout.pipeline import score_stage, scoring  # noqa: E402
from jobscout.pipeline.commute_score import weekly_commute_fit  # noqa: E402
from jobscout.storage import db  # noqa: E402

SOURCES = ("wttj", "france_travail", "linkedin_email")


def _pick_offer(conn, source: str):
    """One scoreable (passed/needs_review) offer for a source, or None.

    Deliberately does NOT filter on ``scored_at``: the smoke test writes
    nothing, so re-scoring an already-scored offer is harmless and keeps the
    test runnable even after a full ``jobscout score`` has covered everything.
    Prefers an un-scored offer when one exists (so a fresh DB shows genuinely
    new work), else falls back to any scoreable offer.
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE source = ? "
        "AND filter_status IN ('passed', 'needs_review') AND scored_at IS NULL "
        "ORDER BY id LIMIT 1",
        (source,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM jobs WHERE source = ? "
        "AND filter_status IN ('passed', 'needs_review') ORDER BY id LIMIT 1",
        (source,),
    ).fetchone()


def _score_offer(row, criteria, ideal, model: str) -> bool:
    """Score one offer for real (writing nothing). True on success.

    Reuses the exact stage logic: the prompt builder (with the assumed-CDI note),
    parse/validate, then the Python commute blend and the pipeline red_flags.
    """
    record = JobRecord.from_row(row)
    assumed_cdi = score_stage._has_assumed_cdi(row["filter_reasons"])
    system, user = scoring.build_prompt(record, criteria, ideal, assumed_cdi=assumed_cdi)

    print(f"    salary={record.salary_text or 'none'!r:<12} "
          f"contract={record.contract_type or 'none'!r} assumed_cdi={assumed_cdi} "
          f"commute_min={row['commute_minutes']}")

    try:
        gen = llm_client.generate(model, user, system=system)
        result = scoring.parse_and_validate(gen.response, criteria)
    except scoring.ScoreValidationError as exc:
        # Mirror the stage: one retry, then it would be needs_review.
        print(f"    [!] invalid JSON on attempt 1 ({exc}); retrying once...")
        try:
            gen = llm_client.generate(model, user, system=system)
            result = scoring.parse_and_validate(gen.response, criteria)
        except scoring.ScoreValidationError as exc2:
            print(f"    [FAIL] invalid JSON twice ({exc2}) -> would be needs_review")
            return False
    except Exception as exc:  # noqa: BLE001 — surface any model/transport error clearly.
        print(f"    [FAIL] {type(exc).__name__}: {exc} -> would be needs_review")
        return False

    fit = weekly_commute_fit(row["commute_minutes"], result.onsite_days)
    flags = list(result.red_flags)
    if fit is None:
        flags.append("commute unknown - pending address resolution")
    if not (record.salary_text or "").strip():
        flags.append("salary not stated")
    total = scoring.compute_total(result.criteria_scores, fit, criteria)

    fit_str = "unknown (dropped)" if fit is None else f"{fit:.1f}"
    print(f"    [OK] total={total:.0f}  onsite={result.onsite_days}  "
          f"remote={result.remote_policy}  commute_fit={fit_str}")
    print(f"         red_flags={flags}")
    return True


def main() -> None:
    prefs = config.load_preferences()
    criteria = scoring.load_criteria(prefs)
    ideal = (prefs.get("scoring_rubric", {}) or {}).get("ideal_role_description", "")
    if not criteria:
        raise SystemExit("No scoring_rubric.criteria in preferences.yaml.")

    model = score_stage.DEFAULT_MODEL
    print(f"Scoring one offer per source with {model} (read-only, writes nothing).\n")

    conn = db.connect()
    try:
        any_scored = False
        all_ok = True
        for source in SOURCES:
            row = _pick_offer(conn, source)
            print(f"### {source}")
            if row is None:
                print("    (no un-scored passed/needs_review offer for this source)\n")
                continue
            print(f"    id={row['id']}  {row['title']}")
            ok = _score_offer(row, criteria, ideal, model)
            any_scored = True
            all_ok = all_ok and ok
            print()
    finally:
        conn.close()

    if not any_scored:
        raise SystemExit(
            "[FAIL] No scoreable (passed/needs_review) offers found for any "
            "source. Run `jobscout ingest` + `jobscout filter` first."
        )
    if all_ok:
        print("[OK] Scoring works end-to-end for every source with an offer. "
              "Safe to run `jobscout score`.")
    else:
        raise SystemExit(
            "[FAIL] At least one source produced an unusable response. Check the "
            "logs/ entry for the failing call and that Ollama has the model pulled."
        )


if __name__ == "__main__":
    main()

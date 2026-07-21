"""LLM-scoring orchestration — the stage after enrichment, before the digest.

For each ``passed``/``needs_review`` offer, this builds the rubric prompt
(``scoring.py``), calls the local model once via ``llm/client.py`` (zero tools),
validates the JSON, computes the Python-owned ``weekly_commute_fit`` sub-score
(``commute_score.py``) from the row's enriched ``commute_minutes`` and the LLM's
inferred ``onsite_days``, blends everything into a weighted 0-100 total, and
persists it. It's the counterpart to ``enrich_commute.py`` and follows the same
skeleton on purpose (this is a learning project — the stages rhyme):

  * **Idempotent by DB state.** "To score" == passed/needs_review AND
    ``scored_at IS NULL``. A re-run only touches newly-passed offers;
    ``--rescore`` re-does all (use after editing the rubric, or once the address
    agent sharpens a commute).
  * **Enrichment-independent.** A row that isn't enriched (NULL
    ``commute_minutes``) is still scored — its commute criterion is flagged
    unknown and dropped from the weighted mean (never zeroed), with a red_flag.
  * **Retry once, then needs_review.** Local models occasionally emit invalid
    JSON. We retry the call once; a second failure marks the offer
    ``score_status = needs_review`` (no crash, no bogus score).
  * **Fail-soft per row.** A malformed record, a model error, or a network blip
    marks that one row failed and the batch continues.
  * **Injectable generate.** The LLM call is a parameter, so every test runs
    fully offline (no Ollama, no network).

SECURITY (CLAUDE.md): the scoring model has zero tools; the untrusted posting is
delimiter-wrapped as data-not-instructions (``scoring.build_prompt``); and no
home location or commute data ever enters the prompt — commute is Python-owned.
Every call is logged in full by ``llm/client.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from jobscout import config
from jobscout.llm import client as llm_client
from jobscout.models import JobRecord
from jobscout.pipeline import scoring
from jobscout.pipeline.commute_score import weekly_commute_fit
from jobscout.pipeline.scoring import ScoreResult, ScoreValidationError
from jobscout.storage import db

logger = logging.getLogger("jobscout.score")

DEFAULT_MODEL = "qwen3.6:35b-a3b"

# The signature of a text-generation call — matches ``llm_client.generate``'s
# (model, prompt, system) usage but lets tests inject a stub returning canned
# JSON. Returns the raw response text.
GenerateFn = Callable[..., "llm_client.GenerationResult"]


@dataclass
class ScoreSummary:
    """The run's tally, rendered by the CLI."""

    dry_run: bool = False
    rescore: bool = False
    considered: int = 0     # rows selected for scoring
    scored: int = 0         # rows we scored and wrote a total for
    needs_review: int = 0   # invalid JSON twice, or model error → flagged
    commute_unknown: int = 0  # of scored, those with no usable commute


def run_score(
    *,
    prefs_path=config.DEFAULT_PREFERENCES_PATH,
    db_path=db.DEFAULT_DB_PATH,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    rescore: bool = False,
    dry_run: bool = False,
    _generate: GenerateFn | None = None,
) -> ScoreSummary:
    """Score passed/needs_review offers with the LLM; blend commute; persist.

    ``dry_run`` builds prompts, calls the model, computes totals and logs them
    but writes nothing to the DB (the LLM-call logs are still written — that's
    the transparency invariant). Returns a ``ScoreSummary`` the CLI renders.
    """
    prefs = config.load_preferences(prefs_path)
    criteria = scoring.load_criteria(prefs)
    ideal = (prefs.get("scoring_rubric", {}) or {}).get("ideal_role_description", "")
    generate = _generate or llm_client.generate
    summary = ScoreSummary(dry_run=dry_run, rescore=rescore)

    conn = db.connect(db_path)
    run_id = None if dry_run else db.record_run_start(conn, dry_run=dry_run)
    try:
        rows = db.select_jobs_to_score(conn, limit=limit, rescore=rescore)
        summary.considered = len(rows)
        for row in rows:
            _score_row(
                conn, row, criteria=criteria, ideal=ideal, model=model,
                generate=generate, dry_run=dry_run, summary=summary,
            )
        if not dry_run:
            conn.commit()
    finally:
        if run_id is not None:
            db.record_run_finish(conn, run_id, {
                "stage": "score",
                "considered": summary.considered,
                "scored": summary.scored,
                "needs_review": summary.needs_review,
                "commute_unknown": summary.commute_unknown,
            })
        conn.close()

    return summary


def _score_row(
    conn, row, *, criteria, ideal, model, generate, dry_run, summary,
) -> None:
    """Score one row, fail-soft. Updates ``summary`` and persists."""
    try:
        record = JobRecord.from_row(row)
        assumed_cdi = _has_assumed_cdi(row["filter_reasons"])
        company_brief = _company_brief(row["company_brief"])
        system, user = scoring.build_prompt(
            record, criteria, ideal, assumed_cdi=assumed_cdi,
            company_brief=company_brief,
        )

        result = _generate_and_validate(generate, model, system, user, criteria)
        if result is None:
            # Invalid JSON twice → needs_review, no bogus score stored.
            summary.needs_review += 1
            logger.info("[%s] %r — needs_review (invalid JSON x2)", row["source"], row["title"])
            if not dry_run:
                db.record_score(
                    conn, row["id"], score_total=None,
                    score_status="needs_review", score_json=None,
                )
            return

        _finalize(result, row, criteria, summary)

        summary.scored += 1
        logger.info(
            "[%s] %r @ %r — score=%.0f onsite=%d%s",
            row["source"], row["title"], row["company"], result.total,
            result.onsite_days,
            " (commute unknown)" if result.commute_fit is None else "",
        )
        if not dry_run:
            db.record_score(
                conn, row["id"], score_total=round(result.total, 1),
                score_status="scored",
                score_json=json.dumps(result.as_json_dict(), ensure_ascii=False),
            )
    except Exception as exc:  # noqa: BLE001 — per-row fail-soft is the point.
        summary.needs_review += 1
        logger.warning(
            "score error [%s] %r: %s", row["source"], row["title"], type(exc).__name__
        )
        if not dry_run:
            db.record_score(
                conn, row["id"], score_total=None,
                score_status="needs_review", score_json=None,
            )


def _generate_and_validate(
    generate, model, system, user, criteria
) -> ScoreResult | None:
    """Call the model, validate; retry once on invalid JSON. None on 2nd failure.

    Only a ``ScoreValidationError`` triggers the retry (a genuinely bad
    response). Any other exception (network, model down) propagates to the
    row-level fail-soft handler.
    """
    for attempt in (1, 2):
        gen = generate(model, user, system=system)
        try:
            return scoring.parse_and_validate(gen.response, criteria)
        except ScoreValidationError as exc:
            logger.info("invalid scoring JSON (attempt %d/2): %s", attempt, exc)
    return None


def _finalize(result: ScoreResult, row, criteria, summary) -> None:
    """Compute the commute sub-score, add pipeline red_flags, blend the total.

    Mutates ``result`` in place: sets ``commute_fit`` and ``total``, appends the
    "commute unknown" / "salary not stated" red_flags. ``row['commute_minutes']``
    is the enrichment stage's one-way time (NULL when unknown/unenriched).
    """
    commute_minutes = row["commute_minutes"]
    result.commute_fit = weekly_commute_fit(commute_minutes, result.onsite_days)
    if result.commute_fit is None:
        summary.commute_unknown += 1
        result.red_flags.append("commute unknown — pending address resolution")

    # Rubric note: an offer with no stated salary shouldn't be tanked, but is
    # flagged for salary clarification (compensation_attractiveness).
    if not (row["salary_text"] or "").strip():
        result.red_flags.append("salary not stated")

    result.total = scoring.compute_total(
        result.criteria_scores, result.commute_fit, criteria
    )


def _company_brief(company_brief_json: str | None) -> dict | None:
    """Parse the row's ``company_brief`` JSON (Phase 3), fail-soft.

    Returns the brief dict for ``scoring.build_prompt`` to fence as advisory
    context, or ``None`` when the offer wasn't researched or the value is
    malformed (the prompt then omits the block entirely). A brief is advisory —
    a parse failure must never block scoring.
    """
    if not company_brief_json:
        return None
    try:
        brief = json.loads(company_brief_json)
    except (json.JSONDecodeError, ValueError):
        return None
    return brief if isinstance(brief, dict) else None


def _has_assumed_cdi(filter_reasons_json: str | None) -> bool:
    """True when the filter stage recorded a 'contract not stated; assumed CDI'.

    Reads the ``filter_reasons`` JSON (a list of ``{filter, outcome, reason}``)
    written by ``filters.FilterVerdict.reasons_json``. Fail-soft: a malformed or
    absent value simply means 'not assumed'.
    """
    if not filter_reasons_json:
        return False
    try:
        reasons = json.loads(filter_reasons_json)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(reasons, list):
        return False
    return any(
        isinstance(r, dict)
        and r.get("filter") == "contract_type"
        and "assumed cdi" in str(r.get("reason", "")).lower()
        for r in reasons
    )

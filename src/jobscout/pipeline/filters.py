"""Deterministic hard filters — the cheap, transparent gate before enrichment.

This is the pure-logic core of Phase 2's hard-filter stage: given one
``JobRecord`` and the loaded ``preferences.yaml``, decide whether the offer
**passes**, needs a **human review**, or is outright **rejected** — and, for
anything that isn't a clean pass, *why*. There is no I/O here; the orchestrator
(``filter_stage.py``) reads records from the DB, calls ``apply_hard_filters``,
and persists the verdict.

The cardinal rule (CLAUDE.md): a *silent* drop of a good offer is the worst
outcome in this project. So:

  * Every rejection carries a human-readable reason and names the filter that
    fired — nothing disappears unexplained.
  * ``needs_review`` and ``rejected`` are distinct. A ``rejected`` is terminal.
    ``needs_review`` is a softer flag (uncertain contract, ambiguous remote
    policy) that keeps the offer alive for a human to look at.
  * Absence is never a rejection. An unstated contract type, an unparseable
    salary, an unknown remote policy → ``needs_review`` or skip, never reject.
  * No filter is written as a bare ``reject if x > cap``: a null/empty/zero
    config value *disables* that filter rather than rejecting everything.

Every checker returns ``FilterReason | None`` (``None`` == satisfied or not
applicable). ``apply_hard_filters`` runs *all* of them — no short-circuit — so
the stored reasons explain everything wrong with an offer, not just the first
thing. The aggregate outcome is still terminal-on-any-reject.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from jobscout import normalize
from jobscout.models import JobRecord
from jobscout.pipeline.salary import parse_salary_range


class Outcome(str, Enum):
    """A single filter's or the whole verdict's outcome.

    A ``str`` Enum so ``.value`` drops straight into the DB's TEXT column and
    round-trips through JSON without a custom encoder.
    """

    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


@dataclass(frozen=True)
class FilterReason:
    """Why one filter contributed a non-passing outcome."""

    filter: str  # "contract_type" | "salary_floor" | "seniority" | ...
    outcome: Outcome
    reason: str  # human-readable, shown in the digest / logs


@dataclass(frozen=True)
class FilterVerdict:
    """The aggregate result of running all hard filters on one offer.

    ``reasons`` holds only the non-passing contributions (a clean pass has an
    empty tuple). ``outcome`` is the terminal-on-reject aggregate.
    """

    outcome: Outcome
    reasons: tuple[FilterReason, ...] = ()

    @property
    def is_rejected(self) -> bool:
        return self.outcome is Outcome.REJECTED

    def reasons_json(self) -> str:
        """Serialize reasons for the ``jobs.filter_reasons`` TEXT column."""
        return json.dumps(
            [
                {"filter": r.filter, "outcome": r.outcome.value, "reason": r.reason}
                for r in self.reasons
            ],
            ensure_ascii=False,
        )


# --------------------------------------------------------------------------
# Individual filters — each pure, each independently testable
# --------------------------------------------------------------------------


def check_contract_type(
    record: JobRecord,
    contract_types: list[str] | None,
    *,
    assume_cdi_when_unstated: bool = False,
) -> FilterReason | None:
    """Keep only offers whose contract type is in ``contract_types``.

    ``record.contract_type`` is already normalized by the adapters (CDI/CDD/…
    or ``None`` when unstated) — we do NOT re-parse it. The non-obvious cases:
      * an empty/absent allowlist → the filter is disabled (everything passes),
        so a config typo can't nuke the run.
      * ``None`` (unstated) — behavior depends on ``assume_cdi_when_unstated``
        (from ``preferences.yaml``):
          - default (``False``) → ``needs_review``, never a reject.
          - ``True`` → **pass, assuming CDI**, but keep an audit-trail reason
            (outcome PASSED, so it doesn't change the verdict) so the digest can
            show "CDI (assumed)" and the scorer knows it wasn't stated. This
            trades a rare false-positive (a CDD slips through to human review,
            where it's caught) for far fewer manual gates — a deliberate owner
            choice, since ~99% of unstated French postings are CDI.
    """
    if not contract_types:
        return None  # filter disabled
    if record.contract_type is None:
        allowed_upper = {c.strip().upper() for c in contract_types}
        if assume_cdi_when_unstated and "CDI" in allowed_upper:
            return FilterReason(
                "contract_type",
                Outcome.PASSED,
                "contract type not stated; assumed CDI",
            )
        return FilterReason(
            "contract_type",
            Outcome.NEEDS_REVIEW,
            "contract type not stated by the source",
        )
    allowed = {c.strip().upper() for c in contract_types}
    if record.contract_type.upper() in allowed:
        return None
    return FilterReason(
        "contract_type",
        Outcome.REJECTED,
        f"contract type {record.contract_type!r} not in {sorted(allowed)}",
    )


def check_salary_floor(
    record: JobRecord, floor_eur: int | None
) -> FilterReason | None:
    """Reject offers whose *stated* salary is below the floor.

    Applies only to offers that state a parseable numeric salary; for a range
    the **upper bound** is compared (so "45–50k" clears a 50k floor). Everything
    else is a skip, never a reject:
      * a falsy floor (``null``/``0``) disables the filter — guards the classic
        "reject if x > cap" trap where a null cap rejects everything;
      * no ``salary_text`` → skip;
      * ``salary_text`` present but unparseable ("Selon profil") → skip. The
        LLM scorer flags "salary not stated" later; we don't reject on it.
    """
    if not floor_eur:
        return None  # filter disabled (null/0 floor)
    if not record.salary_text or not record.salary_text.strip():
        return None  # no stated salary → skip
    parsed = parse_salary_range(record.salary_text)
    if parsed is None:
        return None  # stated but not numeric → skip (scorer flags it)
    if parsed.high >= floor_eur:
        return None
    return FilterReason(
        "salary_floor",
        Outcome.REJECTED,
        f"stated salary upper bound {parsed.high} < floor {floor_eur} "
        f"(from {record.salary_text!r})",
    )


# Seniority tokenization: we split the *original* title on whitespace, hyphens,
# and slashes so "Product Manager / Owner" and "Product-Owner" tokenize into
# their parts, then match whole tokens. Matching on the original title (never
# the normalized dedupe key, never the description) with word boundaries is
# what stops the substring traps CLAUDE.md warns about ("intern" in "internal",
# "PM" in "development").
_TITLE_SPLIT_RE = re.compile(r"[\s/\-]+")
_TITLE_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _title_tokens(title: str) -> set[str]:
    """Lowercased whole-token set of a title, split on space/hyphen/slash."""
    parts = _TITLE_SPLIT_RE.split(title.lower())
    tokens = {_TITLE_PUNCT_RE.sub("", p) for p in parts}
    tokens.discard("")
    return tokens


def _title_normalized_for_phrase(title: str) -> str:
    """Lowered title with hyphens/slashes → spaces, for phrase matching."""
    return re.sub(r"[/\-]+", " ", title.lower())


def _keyword_matches_title(keyword: str, title: str, tokens: set[str]) -> bool:
    """True if ``keyword`` matches the title as a whole token or phrase.

    Single-word keywords ("PM", "junior") match by whole-token membership, so
    "PM" hits "Senior PM" but not "development"/"responsable"/"support".
    Multiword keywords ("product manager", "ai pm", "graduate program") match
    as a whole phrase with word boundaries against the space-normalized title.
    """
    parts = _TITLE_SPLIT_RE.split(keyword.lower().strip())
    parts = [p for p in parts if p]
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in tokens
    phrase = r"\b" + r"\s+".join(re.escape(p) for p in parts) + r"\b"
    return re.search(phrase, _title_normalized_for_phrase(title)) is not None


def check_seniority(
    record: JobRecord,
    include_keywords: list[str] | None,
    exclude_keywords: list[str] | None,
) -> FilterReason | None:
    """Pass a title matching ≥1 include keyword AND 0 exclude keywords.

    Matches whole tokens/phrases against the ORIGINAL ``record.title`` only.
    An exclude match wins if both fire (a "Junior Product Manager" is out even
    though it matches an include). An empty include list disables the filter
    (all pass) so a config typo can't silently reject every offer.
    """
    include_keywords = include_keywords or []
    exclude_keywords = exclude_keywords or []
    if not include_keywords:
        return None  # filter disabled

    title = record.title or ""
    tokens = _title_tokens(title)

    hit_exclude = next(
        (kw for kw in exclude_keywords if _keyword_matches_title(kw, title, tokens)),
        None,
    )
    if hit_exclude is not None:
        return FilterReason(
            "seniority",
            Outcome.REJECTED,
            f"title matches excluded keyword {hit_exclude!r}",
        )

    has_include = any(
        _keyword_matches_title(kw, title, tokens) for kw in include_keywords
    )
    if has_include:
        return None
    return FilterReason(
        "seniority",
        Outcome.REJECTED,
        f"title {title!r} matches no seniority include keyword",
    )


def check_company_blocklist(
    record: JobRecord, blocklist: list[str] | None
) -> FilterReason | None:
    """Reject offers from a blocklisted company (normalized comparison).

    Reuses ``normalize.normalize_company`` on both sides so "Acme" blocks
    "Acme SAS". An empty blocklist disables the filter.
    """
    if not blocklist:
        return None
    blocked = {normalize.normalize_company(c) for c in blocklist}
    blocked.discard("")
    company_key = normalize.normalize_company(record.company)
    if company_key and company_key in blocked:
        return FilterReason(
            "company_blocklist",
            Outcome.REJECTED,
            f"company {record.company!r} is blocklisted",
        )
    return None


# Remote-policy classification from free text (location + description prose).
# Deliberately conservative: when the cues are absent or contradictory we return
# "unknown" (→ needs_review), never a rejection. Coverage is broadened for
# description prose (not just the short location field), since French postings
# usually state the policy in the body ("2 jours de télétravail par semaine",
# "100% présentiel"). Precedence in `classify_remote_policy` — negation, then
# hybrid, then full-remote — resolves overlaps: a posting that says "télétravail"
# AND "2 jours" is hybrid, not full remote.
# An *explicit telework negation* — decisive for onsite even if "télétravail"
# appears (it appears precisely because it's being denied). Kept narrow on
# purpose: only phrasings that actually deny remote work, NOT bare "sur site" /
# "présentiel", which turn up in benefit blurbs ("Sur site, une salle de sport")
# and must not override a real hybrid signal. Those bare words are handled last,
# only when no remote signal exists at all (see `_ONSITE_WORD`).
_REMOTE_ONSITE_NEGATION = re.compile(
    r"(pas de t[ée]l[ée]travail|aucun t[ée]l[ée]travail|sans t[ée]l[ée]travail"
    r"|100\s*%\s*pr[ée]sentiel|t[ée]l[ée]travail\s*:?\s*non"
    r"|no remote|remote\s*:\s*no|not?\s*remote)",
    re.IGNORECASE,
)

# A plain onsite word, used only as a last resort (no remote signal anywhere).
_ONSITE_WORD = re.compile(
    r"(pr[ée]sentiel|sur[- ]site|on[- ]?site|office[- ]based)", re.IGNORECASE
)
# A remote/telework keyword at all. Presence alone means *some* remote is on
# offer; whether it's full or partial is decided by the partiality signal below.
_REMOTE_KEYWORD = re.compile(
    r"(t[ée]l[ée]travail\w*|remote|distanciel)", re.IGNORECASE
)

# An explicit "hybrid/partial" word — decisive for hybrid regardless of counts.
_REMOTE_HYBRID_WORD = re.compile(
    r"(hybrid|hybride|t[ée]l[ée]travail partiel|remote partiel|partial remote"
    r"|remote[- ]friendly)",
    re.IGNORECASE,
)

# A *partiality quantifier*: "N jours", "N à M jours", "deux jours", "N%",
# "N jours par mois/semaine", "50% du temps", "N days per week". When one of
# these co-occurs with a remote keyword, the arrangement is hybrid, not full
# remote — this is what tells "télétravail 2 jours/semaine" apart from
# "full remote". Kept separate from the keyword so ANY word order / separator
# ("Télétravail possible 2 jours", "Télétravail : jusqu'à 3 jours") is caught.
# A *partiality quantifier*: a count of days, or a percentage strictly below
# 100 (100% is full remote, not partial — excluded via the (?!100) guard, and
# capped to a 1–99 shape). "deux jours", "2 à 3 jours", "50% du temps",
# "2 days per week".
_PARTIAL_QUANTIFIER = re.compile(
    r"\b("
    r"(?:un|une|deux|trois|quatre|cinq|\d+)\s*(?:[àa-]\s*\d+\s*)?jours?\b"  # N / deux jours
    r"|(?!100\b)\d{1,2}\s*%\s*(?:du\s*temps)?"                             # 1–99% (not 100%)
    r"|\d\s*days?\s*(?:per\s*week|from\s*home|/\s*week)"                    # 2 days per week
    r")",
    re.IGNORECASE,
)

# Full/unqualified remote signals.
_REMOTE_FULL = re.compile(
    r"(full remote|fully remote|100\s*%?\s*remote|t[ée]l[ée]travail "
    r"(?:complet|total|int[ée]gral)|t[ée]l[ée]travail 100\s*%)",
    re.IGNORECASE,
)


def classify_remote_policy(record: JobRecord) -> str:
    """Return 'remote' | 'hybrid' | 'onsite' | 'unknown' from free text.

    Reads ``location`` + ``description``. Precedence (each step is decisive):

      1. **Onsite negation** ("pas de télétravail", "100% présentiel") → onsite,
         even if the word "télétravail" also appears.
      2. **Explicit hybrid word** ("hybride", "télétravail partiel") → hybrid.
      3. **Remote keyword + a partiality quantifier** ("2 jours de télétravail",
         "télétravailler jusqu'à 3 jours", "50% du temps") → hybrid. Keyword and
         quantifier are matched independently, so any word order or separator
         between them still resolves — the fix for real postings that write
         "Télétravail possible 2 jours" or "Télétravail : jusqu'à 3 jours".
      4. **Explicit full-remote** ("full remote", "100% remote") → remote.
      5. **A bare remote keyword** with no partiality and no full marker →
         remote (an unqualified "télétravail" mention).
      6. A plain onsite word ("présentiel", "sur site") without any remote →
         onsite; otherwise **unknown** (→ needs_review, never a reject).

    Precedence 3-before-4/5 is the key correctness point: a *quantified* remote
    mention is hybrid, so we never mislabel a 2-day-hybrid job as full remote
    (which would zero out its commute in scoring) — the bug this ordering fixes.
    """
    text = f"{record.location or ''} {record.description or ''}"

    # 1. Explicit telework negation wins outright.
    if _REMOTE_ONSITE_NEGATION.search(text):
        return "onsite"

    has_remote = _REMOTE_KEYWORD.search(text) is not None

    # 2-3. Any hybrid word, or a remote keyword paired with a partiality
    # quantifier (in any order / with any separator between them).
    if _REMOTE_HYBRID_WORD.search(text):
        return "hybrid"
    if has_remote and _PARTIAL_QUANTIFIER.search(text):
        return "hybrid"
    # 4-5. Explicit or bare remote with no partiality → full remote.
    if _REMOTE_FULL.search(text) or has_remote:
        return "remote"
    # 6. Only now, with no remote signal at all, does a bare onsite word count —
    # so "Sur site, une salle de sport" in a telework posting can't force onsite.
    if _ONSITE_WORD.search(text):
        return "onsite"
    return "unknown"


def check_remote_policy(
    record: JobRecord, remote_policy: dict | None
) -> FilterReason | None:
    """Reject an offer whose remote policy the owner doesn't accept.

    ``remote_policy`` has ``accept_onsite``/``accept_hybrid``/``accept_remote``
    (default True if absent) and ``min_remote_days_per_week``. A *determined*
    policy the owner rejects → REJECTED; an accepted one → pass; an *unknown*
    policy → ``needs_review`` (never reject — the classifier is fuzzy). A
    positive ``min_remote_days_per_week`` isn't verifiable from free text, so it
    also yields ``needs_review`` rather than a hard reject (flexibility is
    scored, not filtered).
    """
    policy = remote_policy or {}
    accept = {
        "remote": policy.get("accept_remote", True),
        "hybrid": policy.get("accept_hybrid", True),
        "onsite": policy.get("accept_onsite", True),
    }
    classified = classify_remote_policy(record)

    if classified == "unknown":
        return FilterReason(
            "remote_policy",
            Outcome.NEEDS_REVIEW,
            "remote policy could not be determined from the posting",
        )
    if not accept[classified]:
        return FilterReason(
            "remote_policy",
            Outcome.REJECTED,
            f"remote policy {classified!r} is not accepted",
        )
    min_days = policy.get("min_remote_days_per_week") or 0
    if min_days > 0:
        return FilterReason(
            "remote_policy",
            Outcome.NEEDS_REVIEW,
            f"requires ≥{min_days} remote days/week — not verifiable from text",
        )
    return None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _aggregate(reasons: list[FilterReason]) -> Outcome:
    """Terminal-on-reject aggregate: any reject → REJECTED; else any
    needs_review → NEEDS_REVIEW; else PASSED."""
    if any(r.outcome is Outcome.REJECTED for r in reasons):
        return Outcome.REJECTED
    if any(r.outcome is Outcome.NEEDS_REVIEW for r in reasons):
        return Outcome.NEEDS_REVIEW
    return Outcome.PASSED


def apply_hard_filters(record: JobRecord, prefs: dict) -> FilterVerdict:
    """Run every hard filter on one offer and return the aggregate verdict.

    Reads ``prefs["hard_filters"]`` defensively (``.get`` with safe defaults) so
    a partially-filled preferences.yaml never throws mid-run. All checkers run —
    no short-circuit — so the returned ``reasons`` explain everything wrong,
    which the digest surfaces for human review.
    """
    hard = (prefs or {}).get("hard_filters", {}) or {}
    seniority = hard.get("seniority", {}) or {}

    candidates = [
        check_contract_type(
            record,
            hard.get("contract_types"),
            assume_cdi_when_unstated=bool(hard.get("assume_cdi_when_unstated", False)),
        ),
        check_salary_floor(record, hard.get("salary_floor_eur")),
        check_seniority(
            record,
            seniority.get("include_keywords"),
            seniority.get("exclude_keywords"),
        ),
        check_company_blocklist(record, hard.get("company_blocklist")),
        check_remote_policy(record, hard.get("remote_policy")),
    ]
    reasons = [r for r in candidates if r is not None]
    return FilterVerdict(outcome=_aggregate(reasons), reasons=tuple(reasons))

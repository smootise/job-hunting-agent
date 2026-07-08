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
    record: JobRecord, contract_types: list[str] | None
) -> FilterReason | None:
    """Keep only offers whose contract type is in ``contract_types``.

    ``record.contract_type`` is already normalized by the adapters (CDI/CDD/…
    or ``None`` when unstated) — we do NOT re-parse it. The two non-obvious
    cases, both per CLAUDE.md:
      * ``None`` (unstated, common in French CDI-implied postings) →
        ``needs_review``, never a reject.
      * an empty/absent allowlist → the filter is disabled (everything passes),
        so a config typo can't nuke the run.
    """
    if not contract_types:
        return None  # filter disabled
    if record.contract_type is None:
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


# Remote-policy classification from free text. Deliberately conservative: when
# the cues are absent or contradictory we return "unknown" (→ needs_review),
# never a rejection. With the owner's current prefs (accept everything) the only
# live effect is routing genuinely-unknown offers to review; the flags are
# still honored generally so flipping one to false actually filters.
_REMOTE_ONSITE_NEGATION = re.compile(
    r"(pas de t[ée]l[ée]travail|no remote|sur site|sur-site|pr[ée]sentiel|on[ -]?site)",
    re.IGNORECASE,
)
_REMOTE_HYBRID = re.compile(
    r"(hybrid|hybride|t[ée]l[ée]travail partiel|remote partiel|\d\s*j(?:ours?)?\s*(?:de\s*)?t[ée]l[ée]travail)",
    re.IGNORECASE,
)
_REMOTE_FULL = re.compile(
    r"(full remote|100%?\s*remote|t[ée]l[ée]travail (?:complet|total|int[ée]gral)|remote|t[ée]l[ée]travail)",
    re.IGNORECASE,
)


def classify_remote_policy(record: JobRecord) -> str:
    """Return 'remote' | 'hybrid' | 'onsite' | 'unknown' from free text.

    Reads ``location`` + ``description``. Precedence matters: an explicit
    onsite negation ("pas de télétravail", "présentiel") pins onsite even if
    the word "télétravail" appears in it; then hybrid; then remote; else, if a
    plain onsite word appears, onsite; otherwise 'unknown'. When in doubt →
    'unknown', which becomes ``needs_review``, never a reject.
    """
    text = f"{record.location or ''} {record.description or ''}"
    if _REMOTE_ONSITE_NEGATION.search(text):
        return "onsite"
    if _REMOTE_HYBRID.search(text):
        return "hybrid"
    if _REMOTE_FULL.search(text):
        return "remote"
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
        check_contract_type(record, hard.get("contract_types")),
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

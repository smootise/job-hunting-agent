"""Parse a free-text salary string into an annual-gross EUR range.

The one job of this module: turn a source's raw ``salary_text`` into a
``SalaryRange`` whose ``high`` (the range's upper bound) the hard-salary-floor
filter compares against ``salary_floor_eur``. Everything about *why* the upper
bound and not the lower is in CLAUDE.md — a "45–50k" posting must survive a 50k
floor, so we keep the offer if its *upper* bound clears the floor.

Design stance (matches this project): deterministic, transparent, and
**conservative on ambiguity**. When the text doesn't yield a number we trust,
we return ``None`` — which the filter treats as "no stated salary → skip the
floor check", i.e. the offer passes. That is deliberately the safe error: a
wrong number could *silently reject* a good offer (the cardinal sin here),
whereas skipping merely defers the judgement to the LLM scorer, which flags
"salary not stated" of its own accord.

Target unit is **annual gross EUR**. A monthly figure is annualized by the
number of monthly payments per year: 12 by default, or 13/14 when the text
says so ("sur 13 mois" — common in French contracts). For a floor check ×13/14
is the lenient direction (a bigger annual number is easier to clear the floor),
which is intended: we would rather keep a borderline offer for scoring than
drop it.

Real inputs this must handle (from the Phase 1 adapters):
  - WTTJ:           "45000-50000 EUR yearly", "3500-4000 EUR monthly"
  - France Travail: "Annuel de 45000,00 Euros à 55000,00 Euros sur 12 mois",
                    "Mensuel de 4500 Euros", and free text like "Selon profil"
  - LinkedIn:       always None (alerts state no salary)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Lower bound of plausibility, in annual EUR, *after* annualization. A real
# Île-de-France PM salary is well above this; a smaller result is almost
# certainly a misparse (a year like "2025", a reference number, a stray count).
# We discard it and let the offer pass unfiltered rather than risk a wrong
# reject — the safe error, per CLAUDE.md.
_MIN_PLAUSIBLE_ANNUAL = 10000

# Words that signal a string is really about pay, licensing a *bare* number
# (one with no 'k' shorthand) to be read as an amount. Without one of these,
# a lone integer like "2025" in "CDI - 2025" is boilerplate, not a salary.
_SALARY_SIGNAL = re.compile(
    r"(€|\beur\b|\beuros?\b|\bk\b|\bbrut\b|\bgross\b|\bsalair|\bsalary\b"
    r"|\bannuel\b|\bannual\b|\byearly\b|\bmensuel\b|\bmonthly\b|\brémunér)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SalaryRange:
    """A parsed salary, normalized to annual gross EUR.

    ``high`` is what the floor check uses (the range's upper bound). ``low`` is
    ``None`` for a single stated value. ``period``/``months`` record how the
    figure was originally stated and how it was annualized, so a reader (or a
    test) can see the reasoning, not just the result.
    """

    low: int | None
    high: int
    period: str  # "year" | "month" as originally stated (pre-annualization)
    months: int  # 12 | 13 | 14 — monthly payments/year used to annualize
    raw: str


# --------------------------------------------------------------------------
# Amount tokenization
# --------------------------------------------------------------------------

# A single monetary amount. We accept:
#   - a "k"/"K" shorthand:            45k, 45 K, 45k€
#   - grouped thousands:              45 000, 45.000, 45,000
#   - a FR decimal tail:              45000,00  (dropped — cents don't matter)
#   - a plain integer:                45000
# The trailing (?!...) guards keep us from grabbing numbers that are really a
# duration or a rate: "35h", "sur 12 mois", "13 ans", "10%". Month/hour/year
# words immediately after a bare number are the common misparse, so a bare
# number followed by one of those is not treated as an amount.
_AMOUNT_RE = re.compile(
    r"""
    (?P<num>
        \d{1,3}(?:[ .,]\d{3})+(?:,\d{1,2})?   # grouped: 45 000 / 45.000 / 45,000
      | \d+(?:,\d{1,2})?                        # plain: 45000 / 45000,00 / 45
    )
    \s*
    (?P<k>k)?                                    # optional 'k' shorthand
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Words that, when they immediately follow a bare (non-k) number, mean it is
# NOT a salary amount — it's a count of months/hours/years or a percentage.
_UNIT_NOISE_AFTER = re.compile(r"^\s*(h|heures?|mois|ans?|%)\b", re.IGNORECASE)

# Range separators between two amounts: dash variants, or the FR/EN words.
_RANGE_SEP = re.compile(r"\s*(?:-|–|—|à|to|/)\s*", re.IGNORECASE)


def _to_int_amount(num_text: str, has_k: bool) -> int | None:
    """Convert one matched number token's digits to an integer (no k factor).

    Strips a FR/EN decimal tail (cents) and grouping separators so
    "45 000"/"45.000"/"45,000"/"45000,00" all collapse to 45000. The ``k``
    shorthand is applied by the caller (which also propagates it across a
    range), so ``has_k`` is accepted only to keep call sites explicit.
    """
    # Drop a decimal tail first (45000,00 -> 45000): cents never matter for a
    # floor comparison.
    core = re.sub(r"[.,]\d{1,2}$", "", num_text.strip())
    # Then remove grouping separators/spaces.
    digits = re.sub(r"[ .,]", "", core)
    if not digits.isdigit():
        return None
    return int(digits)


def _detect_months(text: str) -> int:
    """How many monthly payments per year the text implies (12 by default).

    French contracts often quote "sur 13 mois" / "14 mois" (13th/14th-month
    bonus). We honor that when annualizing a monthly figure. Absent an explicit
    13/14, we assume 12.
    """
    m = re.search(r"\bsur\s*(13|14)\s*mois\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Also accept a bare "13 mois"/"14 mois" without "sur".
    m = re.search(r"\b(13|14)\s*mois\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 12


def _is_monthly(text: str) -> bool:
    """True when the text states a monthly cadence.

    We look for explicit monthly words (mensuel/mois/month) but NOT the "sur N
    mois" annualization phrase, which describes an *annual* figure paid over N
    months. An annual figure "de 45000 Euros sur 12 mois" is yearly, not
    monthly, so we exclude that pattern first.
    """
    lowered = text.lower()
    # "annuel"/"annual"/"yearly"/"par an" -> explicitly yearly, short-circuit.
    if re.search(r"\b(annuel|annual|yearly|par\s+an|/\s*an|an)\b", lowered):
        # ...unless it *also* says mensuel, which would be contradictory; the
        # explicit yearly word wins (WTTJ/FT both label the period plainly).
        if not re.search(r"\b(mensuel|monthly)\b", lowered):
            return False
    return bool(re.search(r"\b(mensuel|monthly|/\s*mois|par\s+mois)\b", lowered)) or (
        # A bare "de X Euros" with a "mois" cadence and no "annuel" — rare, but
        # "Mensuel de 4500 Euros" is the shape we care about.
        bool(re.search(r"\bmensuel\b", lowered))
    )


def parse_salary_range(text: str | None) -> SalaryRange | None:
    """Parse ``text`` into an annual-gross EUR ``SalaryRange``, or ``None``.

    Returns ``None`` when the text is empty, carries no usable number, or is too
    ambiguous to trust (e.g. "Selon profil", "Competitive", "K€" with no
    figure). ``None`` means "no numeric salary stated" to the caller, which
    then skips the floor check — never a rejection.

    For a range, the two amounts are sorted, so textual order doesn't matter
    ("50-45k" still yields high=50000). For a single value, ``low`` is ``None``.
    """
    if not text or not text.strip():
        return None

    raw = text.strip()

    # Walk every amount-looking token, but drop tokens that are really a
    # duration/rate (bare number immediately followed by h/mois/ans/%). We keep
    # the raw integer and whether the token carried a 'k' so we can propagate
    # the shorthand across a range (see below).
    tokens: list[tuple[int, bool]] = []  # (value_without_k_factor, has_k)
    for m in _AMOUNT_RE.finditer(raw):
        has_k = bool(m.group("k"))
        if not has_k:
            trailing = raw[m.end():]
            if _UNIT_NOISE_AFTER.match(trailing):
                continue  # "12 mois", "35h", "13 ans", "10%" — not an amount
        base = _to_int_amount(m.group("num"), has_k=False)
        if base is None:
            continue
        tokens.append((base, has_k))

    if not tokens:
        return None

    # A bare number is only a salary when the string actually talks about pay.
    # This keeps "CDI - 2025" (a year) or a lone reference number from being
    # read as a salary. If any token carried 'k', that itself is the signal.
    any_k = any(k for _, k in tokens)
    if not any_k and not _SALARY_SIGNAL.search(raw):
        return None

    # 'k' propagates across a range: in "50-45k" the 'k' applies to both
    # operands (50k-45k), not just the one it's attached to. So if any token in
    # a multi-token range has 'k', scale the k-less small operands too.
    amounts: list[int] = []
    for base, has_k in tokens:
        value = base * 1000 if (has_k or (any_k and base < 1000)) else base
        amounts.append(value)

    monthly = _is_monthly(raw)
    months = _detect_months(raw) if monthly else 12

    def annualize(v: int) -> int:
        return v * months if monthly else v

    annual = sorted(annualize(v) for v in amounts)

    # Guardrail: drop implausibly small results (a misparsed reference number,
    # a stray count). If nothing plausible survives, treat as "no salary".
    annual = [v for v in annual if v >= _MIN_PLAUSIBLE_ANNUAL]
    if not annual:
        return None

    high = annual[-1]
    low = annual[0] if len(annual) > 1 else None

    return SalaryRange(
        low=low,
        high=high,
        period="month" if monthly else "year",
        months=months,
        raw=raw,
    )

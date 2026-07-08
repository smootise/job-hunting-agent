"""Shared normalization helpers used by every source adapter.

These live in one module (not copy-pasted per adapter) for two reasons:
consistency — WTTJ, France Travail, and LinkedIn must all detect language
and normalize company/title the *same* way, or dedupe silently fails — and
testability: pure functions with no I/O are trivial to unit-test against the
real sample postings.

A note on scope: the *normalized* company/title strings produced here are
used only as dedupe keys. The human-readable originals stay on the
`JobRecord` untouched. Never show a normalized string in a digest or letter.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Language detection
# --------------------------------------------------------------------------

# A tiny closed-class French stopword set. Closed-class words (articles,
# prepositions, conjunctions) are the highest-signal, lowest-noise language
# tells: they're extremely frequent in real prose and rarely appear in the
# other language. We deliberately avoid a heavyweight langdetect dependency —
# postings are long enough that counting these is plenty accurate, and a
# transparent heuristic is more in keeping with this project than a black box.
_FRENCH_STOPWORDS = frozenset(
    {
        "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou",
        "vous", "nous", "votre", "notre", "pour", "avec", "sur", "dans",
        "au", "aux", "ce", "cette", "ces", "qui", "que", "est", "sont",
        "plus", "chez", "nos", "vos", "en", "par", "son", "sa", "ses",
    }
)

_ENGLISH_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "you", "your", "our", "for", "with",
        "on", "in", "to", "of", "we", "is", "are", "this", "that", "will",
        "as", "at", "by", "from", "their", "our", "these", "those",
    }
)

_WORD_RE = re.compile(r"[a-zàâäéèêëïîôöùûüçœ]+", re.IGNORECASE)


def detect_language(title: str | None, description: str | None) -> str:
    """Return 'fr' or 'en' for a posting.

    Strategy: tokenize title + description, count how many tokens are French
    vs. English closed-class stopwords, and pick the winner. We also give a
    nudge for French diacritics (é, è, à, ç, ...), which English text almost
    never contains — a strong one-directional signal.

    Defaults to 'fr' on a tie or empty input: the search area is Île-de-France,
    so French is the safer prior, and mis-tagging only affects which master
    letter the Phase 3 agent starts from (a reviewable, non-fatal choice).
    """
    text = f"{title or ''} {description or ''}".lower()
    tokens = _WORD_RE.findall(text)
    if not tokens:
        return "fr"

    fr_hits = sum(1 for t in tokens if t in _FRENCH_STOPWORDS)
    en_hits = sum(1 for t in tokens if t in _ENGLISH_STOPWORDS)

    # Diacritics are near-conclusive evidence of French; weight them.
    diacritic_count = len(re.findall(r"[àâäéèêëïîôöùûüçœ]", text))
    fr_hits += diacritic_count

    return "en" if en_hits > fr_hits else "fr"


# --------------------------------------------------------------------------
# Company / title normalization (for dedupe keys only)
# --------------------------------------------------------------------------

# Legal-form suffixes and gender markers that carry no identity information
# but differ between sources ("Acme" vs "Acme SAS" vs "Acme (H/F)"). Stripping
# them lets the same company match across WTTJ and LinkedIn. Matched as whole
# tokens, case-insensitively.
_COMPANY_NOISE_TOKENS = frozenset(
    {"sas", "sasu", "sarl", "sa", "eurl", "se", "group", "groupe",
     "inc", "llc", "ltd", "gmbh", "corp"}
)

# Gender/duplication markers frequently appended to French job titles.
# "(H/F)", "H/F", "F/H", "M/F", "(m/w/d)" etc. We strip them from titles so
# "Product Manager H/F" and "Product Manager" normalize to the same key.
_GENDER_MARKER_RE = re.compile(
    r"\(?\b[hfmwd](?:\s*/\s*[hfmwd])+\b\)?", re.IGNORECASE
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _collapse(text: str) -> str:
    """Lowercase, drop accents-insensitively-friendly noise, collapse spacing."""
    lowered = text.lower().strip()
    # Replace any run of non-alphanumeric chars with a single space so
    # punctuation differences ("l'oreal" vs "loreal") don't split matches.
    return _NON_ALNUM_RE.sub(" ", lowered).strip()


def normalize_company(name: str | None) -> str:
    """Normalize a company name into a dedupe key.

    Lowercases, strips legal-form suffixes (SAS, SARL, GmbH, ...), and
    collapses punctuation/whitespace. Returns "" for None/empty so callers
    can treat "no company" uniformly. Result is a key, NOT a display value.
    """
    if not name:
        return ""
    collapsed = _collapse(name)
    tokens = [t for t in collapsed.split() if t not in _COMPANY_NOISE_TOKENS]
    return " ".join(tokens)


def normalize_title(title: str | None) -> str:
    """Normalize a job title into a dedupe key.

    Strips gender markers ((H/F), M/F, m/w/d), lowercases, and collapses
    punctuation/whitespace. Crucially this is used ONLY as a dedupe key —
    seniority keyword matching (Phase 2) works on the *original* title with
    whole-token boundaries, never on this collapsed form, precisely to avoid
    the substring traps CLAUDE.md warns about (e.g. 'intern' in 'internal').
    """
    if not title:
        return ""
    without_gender = _GENDER_MARKER_RE.sub(" ", title)
    return _collapse(without_gender)


# --------------------------------------------------------------------------
# Contract type
# --------------------------------------------------------------------------

# Maps the many ways sources spell a contract onto a small normalized set.
# The key is a lowercased token/phrase found in the source; the value is our
# canonical label. Anything not found here → None (unstated), which Phase 2
# treats as needs_review rather than a rejection.
_CONTRACT_ALIASES = {
    "cdi": "CDI",
    "permanent": "CDI",
    "full_time": None,  # WTTJ conflates schedule with contract; not decisive
    "cdd": "CDD",
    "temporary": "CDD",
    "fixed-term": "CDD",
    "fixed term": "CDD",
    "mis": "interim",
    "interim": "interim",
    "intérim": "interim",
    "freelance": "freelance",
    "independent": "freelance",
    "internship": "internship",
    "stage": "internship",
    "stagiaire": "internship",
    "apprenticeship": "apprenticeship",
    "alternance": "apprenticeship",
    "apprentissage": "apprenticeship",
}


def parse_contract_type(raw: str | None) -> str | None:
    """Map a source's native contract string to a canonical label, or None.

    Returns None when the input is empty OR when it maps to no decisive
    contract (e.g. WTTJ's 'full_time', which describes hours, not contract
    type). None is meaningful: Phase 2's hard filter marks such offers
    needs_review instead of dropping them — rejecting on *absence* of a stated
    contract is a false-negative risk this project explicitly avoids.
    """
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _CONTRACT_ALIASES:
        return _CONTRACT_ALIASES[key]
    # Substring fallback: some sources embed the term in a phrase
    # ("Contrat à durée indéterminée (CDI)"). Whole-word check to avoid
    # matching 'cdd' inside unrelated text.
    for alias, canonical in _CONTRACT_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", key):
            return canonical
    return None

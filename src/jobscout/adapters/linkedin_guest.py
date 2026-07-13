"""LinkedIn public *guest* job-posting endpoint — description backfill.

The LinkedIn job-alert *emails* (see ``linkedin_email.py``) carry only
title/company/location/URL — no description, no contract. That leaves every
LinkedIn offer at ``needs_review`` and, worse, gives the LLM scorer nothing to
read. This module fills that gap using the **public, no-login guest endpoint**
LinkedIn itself serves:

    https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}

This is the "public guest endpoint, low volume" path the project brief and
CLAUDE.md explicitly permit — no account, no login, no browser automation, no
scraping of authenticated pages. It is rate-limited, so the *orchestration*
(``pipeline/enrich_linkedin.py``) fetches conservatively (sequential, jittered
delay, per-run cap, cached, fail-soft). This module is the **pure parser** for
one fetched page: HTML in, structured fields out, no I/O — unit-tested against a
saved fixture exactly like ``parse_alert_email``.

What the page reliably contains (verified against real postings):
  * the full description prose (``.show-more-less-html__markup``), and
  * a small structured criteria list — ``Employment type`` (a *schedule*:
    "Full-time"), ``Seniority level``, ``Job function``, ``Industries``.

A deliberate non-inference: LinkedIn's ``Employment type`` is a schedule, not a
French contract type. "Full-time" is NOT "CDI". So this parser surfaces it as
``employment_type`` and lets the caller decide — it never fabricates a CDI the
posting didn't state (that would silently flip a needs_review into a pass on
invented data, the exact false-positive the pipeline avoids).
"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup

# The guest JSON-ish endpoint actually returns an HTML fragment. Multiple class
# names appear across LinkedIn's A/B variants; we try each so a markup tweak
# doesn't silently break extraction.
_DESCRIPTION_SELECTORS = (
    ".show-more-less-html__markup",
    ".description__text",
)
_CRITERIA_ITEM_SELECTORS = (
    ".description__job-criteria-item",
    ".job-criteria__item",
)
_CRITERIA_HEADER_SELECTORS = (
    ".description__job-criteria-subheader",
    ".job-criteria__subheader",
    "h3",
)
_CRITERIA_VALUE_SELECTORS = (
    ".description__job-criteria-text",
    ".job-criteria__text",
)


@dataclass(frozen=True)
class GuestPosting:
    """The fields we can pull from one guest job-posting page.

    Every field is optional: LinkedIn omits criteria on some postings, and a
    blocked/partial response yields nothing. ``None`` everywhere is a valid
    result the caller treats as "nothing to backfill" (fail-soft), never an
    error.
    """

    description: str | None
    employment_type: str | None  # schedule ("Full-time"), NOT a contract type
    seniority_level: str | None
    job_function: str | None
    industries: str | None


def _first(soup: BeautifulSoup, selectors: tuple[str, ...]):
    for sel in selectors:
        el = soup.select_one(sel)
        if el is not None:
            return el
    return None


def _first_all(soup: BeautifulSoup, selectors: tuple[str, ...]):
    for sel in selectors:
        found = soup.select(sel)
        if found:
            return found
    return []


def parse_job_posting(html: str | None) -> GuestPosting:
    """Parse one guest job-posting page's HTML into a ``GuestPosting`` (pure).

    Returns an all-``None`` ``GuestPosting`` for empty/blocked/unrecognized
    HTML rather than raising — the enrichment step is fail-soft, so a bad page
    must simply leave the row unenriched (still ``needs_review``), never crash
    the batch.
    """
    if not html or not html.strip():
        return GuestPosting(None, None, None, None, None)

    soup = BeautifulSoup(html, "html.parser")

    desc_el = _first(soup, _DESCRIPTION_SELECTORS)
    description = None
    if desc_el is not None:
        # Preserve paragraph/line breaks so the description stays readable for
        # the scorer; collapse only within lines.
        text = desc_el.get_text("\n", strip=True)
        description = text or None

    criteria = _parse_criteria(soup)

    return GuestPosting(
        description=description,
        employment_type=criteria.get("employment type"),
        seniority_level=criteria.get("seniority level"),
        job_function=criteria.get("job function"),
        industries=criteria.get("industries"),
    )


def _parse_criteria(soup: BeautifulSoup) -> dict[str, str]:
    """Read the structured criteria list into a lowercased-key dict.

    Keys are lowercased header labels ("employment type", "seniority level").
    Robust to the two known markup variants; skips any item missing a header or
    value.
    """
    out: dict[str, str] = {}
    for item in _first_all(soup, _CRITERIA_ITEM_SELECTORS):
        header = None
        for sel in _CRITERIA_HEADER_SELECTORS:
            h = item.select_one(sel)
            if h is not None:
                header = h.get_text(strip=True)
                break
        value = None
        for sel in _CRITERIA_VALUE_SELECTORS:
            v = item.select_one(sel)
            if v is not None:
                value = v.get_text(strip=True)
                break
        if header and value:
            out[header.lower()] = value
    return out

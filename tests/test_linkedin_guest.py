"""Tests for the LinkedIn guest job-posting parser.

Pure parsing against a saved real fixture (a public posting — safe to commit,
unlike the personal-data .eml). Locks in that we extract the description and
the structured criteria, and — critically — that we surface LinkedIn's
"Full-time" as an *employment type* (a schedule), never as a contract type.
"""

from __future__ import annotations

from pathlib import Path

from jobscout.adapters.linkedin_guest import GuestPosting, parse_job_posting

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_guest_jobposting.html"


def test_parses_description_and_criteria():
    html = FIXTURE.read_text(encoding="utf-8")
    result = parse_job_posting(html)

    assert result.description is not None
    assert len(result.description) > 200  # real prose, not a stub
    assert result.employment_type == "Full-time"
    assert result.seniority_level == "Mid-Senior level"
    assert result.job_function == "Product Management"


def test_employment_type_is_not_a_contract_type():
    # The deliberate non-inference: "Full-time" is a schedule. The parser must
    # not present it as CDI/CDD — the caller maps it (and full_time -> None).
    result = parse_job_posting(FIXTURE.read_text(encoding="utf-8"))
    assert result.employment_type not in {"CDI", "CDD", "interim"}


def test_empty_html_returns_all_none():
    result = parse_job_posting("")
    assert result == GuestPosting(None, None, None, None, None)


def test_unrecognized_html_returns_all_none():
    # A blocked/garbage response must not raise — fail-soft.
    result = parse_job_posting("<html><body>nothing useful</body></html>")
    assert result.description is None
    assert result.employment_type is None


def test_none_html_returns_all_none():
    assert parse_job_posting(None) == GuestPosting(None, None, None, None, None)

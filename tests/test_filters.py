"""Tests for the deterministic hard filters.

These lock in the CLAUDE.md semantics whose failure mode is a *silent* wrong
drop: whole-token seniority matching (the substring traps), the salary
upper-bound rule, the null/0-disables-filter rule, and the
absence-is-needs-review-not-reject rule. If any of these regress, a good offer
dies invisibly — the worst outcome in this project — so each trap is a test.
"""

from __future__ import annotations

from jobscout.models import JobRecord
from jobscout.pipeline import filters
from jobscout.pipeline.filters import Outcome


def make_record(
    *,
    title="Product Manager",
    company="Acme",
    location="Paris",
    contract_type="CDI",
    salary_text=None,
    description=None,
) -> JobRecord:
    """Build a JobRecord with sensible defaults; override per test."""
    return JobRecord(
        source="test",
        external_id="1",
        url="https://example.com/1",
        title=title,
        company=company,
        location=location,
        contract_type=contract_type,
        salary_text=salary_text,
        description=description,
        posted_at=None,
        lang="en",
    )


# --------------------------------------------------------------------------
# Seniority — the substring traps (the reason CLAUDE.md is emphatic here)
# --------------------------------------------------------------------------

INCLUDE = [
    "product manager", "product owner", "technical product manager",
    "ai product manager", "senior product manager", "PM", "PO", "AI PM", "AI PO",
]
EXCLUDE = ["junior", "stage", "stagiaire", "intern", "internship", "alternance",
           "graduate program"]


def seniority(title):
    return filters.check_seniority(make_record(title=title), INCLUDE, EXCLUDE)


def test_seniority_international_survives_intern_exclude():
    # "intern" must NOT substring-match "International" — the classic trap.
    assert seniority("Product Manager, International") is None  # pass


def test_seniority_pm_does_not_match_development():
    # "PM"/"PO" must not substring-match "development"/"responsable"/"support".
    r = seniority("Software Development Engineer")
    assert r is not None and r.outcome is Outcome.REJECTED


def test_seniority_responsable_support_rejected():
    r = seniority("Responsable Support")
    assert r is not None and r.outcome is Outcome.REJECTED


def test_seniority_senior_pm_passes():
    assert seniority("Senior PM") is None


def test_seniority_slash_split_passes():
    # "Product Manager / Owner" tokenizes across the slash → include matches.
    assert seniority("Product Manager / Owner") is None


def test_seniority_junior_excluded_even_with_include():
    r = seniority("Junior Product Manager")
    assert r is not None and r.outcome is Outcome.REJECTED
    assert "junior" in r.reason


def test_seniority_staging_not_matched_by_stage():
    # "Staging Engineer" must be rejected as NO-INCLUDE, not caught by "stage".
    r = seniority("Staging Engineer")
    assert r is not None and r.outcome is Outcome.REJECTED
    assert "no seniority include" in r.reason


def test_seniority_ai_product_manager_passes():
    assert seniority("AI Product Manager") is None


def test_seniority_never_reads_description():
    # Include phrase only in the description → still rejected (title-only match).
    rec = make_record(title="Chef de Projet", description="a product manager role")
    r = filters.check_seniority(rec, INCLUDE, EXCLUDE)
    assert r is not None and r.outcome is Outcome.REJECTED


def test_seniority_matches_original_title_with_gender_marker():
    assert seniority("Product Manager (H/F)") is None


def test_seniority_empty_include_disables_filter():
    # A config typo emptying the include list must not reject everything.
    assert filters.check_seniority(make_record(title="Chef de Projet"), [], EXCLUDE) is None


# --------------------------------------------------------------------------
# Contract type
# --------------------------------------------------------------------------


def test_contract_cdi_passes():
    assert filters.check_contract_type(make_record(contract_type="CDI"), ["CDI"]) is None


def test_contract_cdd_rejected():
    r = filters.check_contract_type(make_record(contract_type="CDD"), ["CDI"])
    assert r is not None and r.outcome is Outcome.REJECTED


def test_contract_none_is_needs_review_not_reject():
    r = filters.check_contract_type(make_record(contract_type=None), ["CDI"])
    assert r is not None and r.outcome is Outcome.NEEDS_REVIEW


def test_contract_empty_list_disables_filter():
    assert filters.check_contract_type(make_record(contract_type="CDD"), []) is None


def test_contract_case_insensitive():
    assert filters.check_contract_type(make_record(contract_type="CDI"), ["cdi"]) is None


# --------------------------------------------------------------------------
# Salary floor (via the filter — parser has its own suite)
# --------------------------------------------------------------------------


def test_salary_none_passes():
    assert filters.check_salary_floor(make_record(salary_text=None), 50000) is None


def test_salary_range_upper_bound_kept():
    # "45-50k" against a 50k floor is KEPT (upper bound clears it).
    assert filters.check_salary_floor(make_record(salary_text="45-50k"), 50000) is None


def test_salary_below_floor_rejected():
    r = filters.check_salary_floor(make_record(salary_text="40-45k"), 50000)
    assert r is not None and r.outcome is Outcome.REJECTED


def test_salary_vague_text_passes():
    assert filters.check_salary_floor(make_record(salary_text="Selon profil"), 50000) is None


def test_salary_floor_none_or_zero_disables_filter():
    # The null/0 trap: a falsy floor must disable, not reject everything.
    assert filters.check_salary_floor(make_record(salary_text="30k"), None) is None
    assert filters.check_salary_floor(make_record(salary_text="30k"), 0) is None


# --------------------------------------------------------------------------
# Company blocklist
# --------------------------------------------------------------------------


def test_blocklist_normalized_match_rejects():
    r = filters.check_company_blocklist(make_record(company="Acme SAS"), ["Acme"])
    assert r is not None and r.outcome is Outcome.REJECTED


def test_blocklist_empty_disables():
    assert filters.check_company_blocklist(make_record(company="Acme"), []) is None


# --------------------------------------------------------------------------
# Remote policy
# --------------------------------------------------------------------------

ACCEPT_ALL = {"accept_onsite": True, "accept_hybrid": True, "accept_remote": True,
              "min_remote_days_per_week": 0}


def test_remote_unknown_is_needs_review():
    r = filters.check_remote_policy(make_record(location="Paris"), ACCEPT_ALL)
    assert r is not None and r.outcome is Outcome.NEEDS_REVIEW


def test_remote_rejected_when_not_accepted():
    policy = {**ACCEPT_ALL, "accept_remote": False}
    r = filters.check_remote_policy(make_record(location="Full remote"), policy)
    assert r is not None and r.outcome is Outcome.REJECTED


def test_remote_accepted_passes():
    assert filters.check_remote_policy(make_record(location="Full remote"), ACCEPT_ALL) is None


def test_onsite_rejected_when_not_accepted():
    policy = {**ACCEPT_ALL, "accept_onsite": False}
    r = filters.check_remote_policy(make_record(location="Présentiel, Paris"), policy)
    assert r is not None and r.outcome is Outcome.REJECTED


def test_min_remote_days_positive_is_needs_review():
    policy = {**ACCEPT_ALL, "min_remote_days_per_week": 2}
    r = filters.check_remote_policy(make_record(location="hybride"), policy)
    assert r is not None and r.outcome is Outcome.NEEDS_REVIEW


# --------------------------------------------------------------------------
# Aggregation + end-to-end apply_hard_filters
# --------------------------------------------------------------------------

PREFS = {
    "hard_filters": {
        "contract_types": ["CDI"],
        "salary_floor_eur": 50000,
        "seniority": {"include_keywords": INCLUDE, "exclude_keywords": EXCLUDE},
        "company_blocklist": [],
        "remote_policy": ACCEPT_ALL,
    }
}


def test_aggregate_reject_wins_over_needs_review():
    # CDD (reject) + unknown remote (needs_review) → REJECTED, both reasons kept.
    rec = make_record(title="Product Manager", contract_type="CDD", location="Paris")
    v = filters.apply_hard_filters(rec, PREFS)
    assert v.outcome is Outcome.REJECTED
    fired = {r.filter for r in v.reasons}
    assert "contract_type" in fired and "remote_policy" in fired


def test_aggregate_two_needs_review():
    # Unstated contract + unknown remote → NEEDS_REVIEW with two reasons.
    rec = make_record(title="Senior PM", contract_type=None, location="Paris")
    v = filters.apply_hard_filters(rec, PREFS)
    assert v.outcome is Outcome.NEEDS_REVIEW
    assert len(v.reasons) == 2


def test_aggregate_clean_pass_has_no_reasons():
    rec = make_record(title="Senior Product Manager", contract_type="CDI",
                      location="Full remote", salary_text="55-60k")
    v = filters.apply_hard_filters(rec, PREFS)
    assert v.outcome is Outcome.PASSED
    assert v.reasons == ()


def test_current_prefs_clean_cdi_pm_paris_no_salary_is_needs_review():
    # The real default: a clean CDI Senior PM in Paris with no stated salary and
    # no remote signal lands in NEEDS_REVIEW (remote unknown), never rejected.
    rec = make_record(title="Senior Product Manager", contract_type="CDI",
                      location="Paris", salary_text=None)
    v = filters.apply_hard_filters(rec, PREFS)
    assert v.outcome is Outcome.NEEDS_REVIEW
    assert [r.filter for r in v.reasons] == ["remote_policy"]


def test_verdict_reasons_json_roundtrip():
    rec = make_record(contract_type="CDD", location="Paris")
    v = filters.apply_hard_filters(rec, PREFS)
    import json
    parsed = json.loads(v.reasons_json())
    assert all({"filter", "outcome", "reason"} <= set(item) for item in parsed)

"""Tests for the salary-range parser.

The salary floor is the one hard filter that reads a *number* out of free
text, so a bug here silently rejects (a misparse producing a low number) or
silently keeps (a missed number) offers. These tests pin the CLAUDE.md
semantics: compare the range's UPPER bound, annualize monthly figures (×12,
or ×13/14 when stated), and return None — never a wrong number — on anything
ambiguous.
"""

from __future__ import annotations

import pytest

from jobscout.pipeline.salary import parse_salary_range


def test_wttj_yearly_range_upper_bound():
    r = parse_salary_range("45000-50000 EUR yearly")
    assert r is not None
    assert r.low == 45000
    assert r.high == 50000  # the floor check compares this
    assert r.period == "year"


def test_k_shorthand_range_keeps_upper_bound():
    # "45-50k" against a 50k floor must be KEPT -> upper bound is 50000.
    r = parse_salary_range("45-50k")
    assert r is not None
    assert r.high == 50000


def test_french_grouped_range_with_a():
    r = parse_salary_range("45 000 à 55 000 euros")
    assert r is not None
    assert r.high == 55000


def test_france_travail_annual_label():
    r = parse_salary_range(
        "Annuel de 45000,00 Euros à 55000,00 Euros sur 12 mois"
    )
    assert r is not None
    assert r.low == 45000
    assert r.high == 55000
    assert r.period == "year"  # "sur 12 mois" describes an annual figure


def test_single_value_has_no_low():
    r = parse_salary_range("50000 EUR yearly")
    assert r is not None
    assert r.low is None
    assert r.high == 50000


def test_monthly_range_annualized_x12():
    # 3500-4000/month -> 42000-48000/year; upper bound 48000 is below a 50k floor.
    r = parse_salary_range("3500-4000 EUR monthly")
    assert r is not None
    assert r.period == "month"
    assert r.high == 48000


def test_monthly_x13_crosses_floor():
    # 4000/month x13 = 52000/year -> would clear a 50k floor that x12 (48000)
    # would not. Pins the honor-13-months decision.
    r = parse_salary_range("4000 Euros mensuel sur 13 mois")
    assert r is not None
    assert r.months == 13
    assert r.high == 52000


def test_monthly_x14():
    r = parse_salary_range("Mensuel de 4000 Euros sur 14 mois")
    assert r is not None
    assert r.months == 14
    assert r.high == 56000


def test_range_sorts_regardless_of_textual_order():
    r = parse_salary_range("50-45k")
    assert r is not None
    assert r.low == 45000
    assert r.high == 50000


@pytest.mark.parametrize("text", ["Selon profil", "Competitive", "à négocier", "N/A", "", None])
def test_no_number_returns_none(text):
    assert parse_salary_range(text) is None


@pytest.mark.parametrize("text", ["35h hebdomadaire", "CDI - 2025", "sur 12 mois", "13 ans d'expérience"])
def test_spurious_numbers_return_none(text):
    # Durations, hour counts, years of experience, a lone month count — none of
    # these are a salary; a naive digit grab would misread them.
    assert parse_salary_range(text) is None


def test_k_symbol_without_figure_returns_none():
    assert parse_salary_range("K€") is None

"""Tests for the shared normalization helpers.

These lock in the CLAUDE.md semantics that cause *silent* failures when wrong:
language detection (drives which master letter is used), company/title
normalization (drives dedupe), and the unstated-contract->None rule (drives
whether an offer survives Phase 2).
"""

from __future__ import annotations

from pathlib import Path

from jobscout import normalize

SAMPLES = Path(__file__).parent.parent / "samples"


def test_detect_language_french_posting():
    text = (SAMPLES / "NEXTON_posting_FR.txt").read_text(encoding="utf-8")
    assert normalize.detect_language("Product Owner Data & IA", text) == "fr"


def test_detect_language_english_posting():
    text = (SAMPLES / "Dataiku_posting_EN.txt").read_text(encoding="utf-8")
    assert normalize.detect_language("Product Manager", text) == "en"


def test_detect_language_defaults_to_french_on_empty():
    assert normalize.detect_language(None, None) == "fr"


def test_normalize_company_strips_legal_suffix():
    # SAS / SARL / GmbH are noise; the same firm should collapse to one key.
    assert normalize.normalize_company("Acme SAS") == normalize.normalize_company("Acme")
    assert normalize.normalize_company("Foo GmbH") == "foo"


def test_normalize_company_collapses_punctuation():
    # Apostrophes/punctuation collapse to a single space so "L'Oreal" and
    # "L Oreal" produce the same dedupe key. (Accents are preserved — the
    # samples in this test use the un-accented spelling.)
    assert normalize.normalize_company("L'Oreal") == "l oreal"
    assert normalize.normalize_company("L'Oreal") == normalize.normalize_company("L Oreal")


def test_normalize_title_strips_gender_marker():
    assert normalize.normalize_title("Product Manager H/F") == \
        normalize.normalize_title("Product Manager")
    assert normalize.normalize_title("Développeur (F/H)") == \
        normalize.normalize_title("Développeur")


def test_parse_contract_type_maps_known_values():
    assert normalize.parse_contract_type("CDI") == "CDI"
    assert normalize.parse_contract_type("permanent") == "CDI"
    assert normalize.parse_contract_type("CDD") == "CDD"
    assert normalize.parse_contract_type("alternance") == "apprenticeship"


def test_parse_contract_type_unstated_is_none():
    # The cardinal rule: absence of a stated contract -> None (needs_review),
    # never a silent reject. 'full_time' is a schedule, not a contract.
    assert normalize.parse_contract_type(None) is None
    assert normalize.parse_contract_type("") is None
    assert normalize.parse_contract_type("full_time") is None
    assert normalize.parse_contract_type("some unknown term") is None

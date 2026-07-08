"""Tests for each adapter's *parser* against saved fixtures — no network.

We test the pure mapping (native payload -> JobRecord), not the live fetch:
the fixtures were captured once from the real APIs (WTTJ, France Travail) and
a scrubbed synthetic LinkedIn email. This keeps the suite fully offline and
reproducible (the "fully local" rule) while still exercising the exact shapes
the real sources return.
"""

from __future__ import annotations

import json

from jobscout.adapters import france_travail, linkedin_email, wttj


def test_wttj_parse_hit(fixtures_dir):
    data = json.loads((fixtures_dir / "wttj_search.json").read_text(encoding="utf-8"))
    hit = data["results"][0]["hits"][0]
    record = wttj.parse_hit(hit)

    assert record.source == "wttj"
    assert record.external_id  # slug/objectID present
    assert record.title
    assert record.company
    assert record.url.startswith("https://www.welcometothejungle.com/")
    # WTTJ's contract_type_names.fr should have mapped to a canonical label.
    assert record.contract_type in {"CDI", "CDD", "interim", "internship",
                                     "apprenticeship", "freelance", None}


def test_france_travail_parse_offer(fixtures_dir):
    data = json.loads(
        (fixtures_dir / "france_travail_search.json").read_text(encoding="utf-8")
    )
    offer = data["resultats"][0]
    record = france_travail.parse_offer(offer)

    assert record.source == "france_travail"
    assert record.external_id == str(offer["id"])
    assert record.title == offer["intitule"]
    assert record.company  # entreprise.nom
    assert record.lang == "fr"


def test_linkedin_parse_native_alert(fixtures_dir):
    raw = (fixtures_dir / "linkedin_alert_synthetic.eml").read_bytes()
    records = linkedin_email.parse_alert_email(raw)

    assert len(records) == 3
    ids = {r.external_id for r in records}
    assert ids == {"4400000001", "4400000002", "4400000003"}
    first = next(r for r in records if r.external_id == "4400000001")
    assert first.title == "Senior Product Manager"
    assert first.company == "Acme SaaS"
    assert first.location == "Paris (Hybrid)"
    assert first.url == "https://www.linkedin.com/jobs/view/4400000001/"
    # Alerts never state contract/salary -> None (needs_review downstream).
    assert first.contract_type is None
    assert first.salary_text is None


def test_linkedin_parse_forwarded_alert(fixtures_dir):
    """A forwarded alert must parse the same jobs as the native one — the
    nested LinkedIn HTML is what matters, not the forward wrapper."""
    raw = (fixtures_dir / "linkedin_alert_forwarded_synthetic.eml").read_bytes()
    records = linkedin_email.parse_alert_email(raw)

    assert {r.external_id for r in records} == {
        "4400000001", "4400000002", "4400000003"
    }


def test_linkedin_parse_ignores_non_alert_email():
    """A stray non-alert email in the folder yields [] rather than raising."""
    raw = b"From: a@b.com\r\nSubject: hi\r\nContent-Type: text/plain\r\n\r\nno jobs here\r\n"
    assert linkedin_email.parse_alert_email(raw) == []

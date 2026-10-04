"""Route smoke tests for the webapp (web/app.py + routes.py).

Offline: a temp DB seeded with one scored + one unscored offer, and
Settings pointed at the committed preferences.example.yaml so criteria rendering
works without the gitignored real file. Asserts the read-only contract (GET-only
routes, the fragment is a bare table, 404 for a missing id) and that the detail
page renders the score + company blocks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.models import JobRecord
from jobscout.storage import db, queries
from jobscout.web.app import create_app
from jobscout.web.settings import Settings

_PREFS = Path(__file__).resolve().parents[1] / "preferences.example.yaml"


def _job(ext, **kw):
    d = dict(
        source="wttj", external_id=ext, url=f"https://x/{ext}", title="Product Manager",
        company="ACME", location="Paris", contract_type="CDI", salary_text=None,
        description="A PM role.", posted_at=None, lang="fr",
    )
    d.update(kw)
    return JobRecord(**d)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = db.connect(db_path)
    db.upsert_jobs(conn, [_job("a"), _job("b", company="BetaCorp")])
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM jobs")}
    db.record_filter_verdict(conn, ids["a"], filter_status="passed", filter_reasons_json="[]")
    db.record_filter_verdict(conn, ids["b"], filter_status="passed", filter_reasons_json="[]")
    db.record_score(
        conn, ids["a"], score_total=82.5, score_status="scored",
        score_json=json.dumps({
            "reasoning": "Strong fit for a B2B SaaS PM.",
            "red_flags": ["salary not stated"],
            "criteria_scores": {"product_culture_and_management": 8},
            "weekly_commute_fit": 6.0, "onsite_days": 3, "remote_policy": "hybrid",
            "commute_included_in_total": True,
        }),
    )
    db.record_company_brief(conn, ids["a"], company_brief_json=json.dumps(
        {"summary": "ACME builds AI workflow tools.", "product": "Automation platform"}))
    conn.commit()
    conn.close()

    settings = Settings(
        project_root=tmp_path, db_path=db_path, preferences_path=_PREFS,
        env_path=tmp_path / ".env",
    )
    # Context-manager form runs the lifespan, so app.state.runner exists (the
    # dashboard/detail routes read runner.snapshot()). The worker thread idles
    # until something is enqueued, so this is cheap for these read-only tests.
    with TestClient(create_app(settings)) as tc:
        yield tc, ids


def test_healthz(client):
    tc, _ = client
    r = tc.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_dashboard_shows_total(client):
    tc, _ = client
    r = tc.get("/")
    assert r.status_code == 200
    assert "Total offers" in r.text and ">2<" in r.text.replace(" ", "").replace("\n", "")


def test_offers_list_ok(client):
    tc, _ = client
    r = tc.get("/offers")
    assert r.status_code == 200
    assert "Product Manager" in r.text and "BetaCorp" in r.text


def test_offers_table_fragment_is_bare(client):
    tc, _ = client
    r = tc.get("/offers/table?sort=score_total&dir=desc")
    assert r.status_code == 200
    assert "<table" in r.text
    assert "<html" not in r.text.lower()  # a fragment, not a full page


def test_offers_table_sort_by_criterion(client):
    tc, _ = client
    # role_scope_and_focus exists in the committed preferences.example.yaml stub.
    r = tc.get("/offers/table?sort=criteria:role_scope_and_focus")
    assert r.status_code == 200


def test_offers_table_bad_sort_is_400(client):
    tc, _ = client
    r = tc.get("/offers/table?sort=nonsense")
    assert r.status_code == 400


def test_eudate_filter_formats_day_first():
    """dd/mm/yyyy — unambiguous for the owner, and 13/… can't be misread as US."""
    from jobscout.web.templating import _eudate

    assert _eudate("2026-09-21T17:29:39.388555+00:00") == "21/09/2026"
    assert _eudate("2026-01-05") == "05/01/2026"       # zero-padded
    assert _eudate(None) == "" and _eudate("") == ""
    assert _eudate("not a date") == "not a date"       # best-effort, never raises


def test_date_filters_render_eu_text_plus_iso_field(client):
    """Each date filter is an EU-formatted text box + a hidden ISO date input.

    The visible text is dd/mm/yyyy on every browser (Firefox/Safari ignore the
    document `lang`), while the *named* field keeps carrying ISO so the route's
    validator and every existing URL still work.
    """
    tc, _ = client
    r = tc.get("/offers?posted_after=2026-09-21")
    # The named (submitted) field is still ISO...
    assert 'type="date" name="posted_after" value="2026-09-21"' in r.text
    # ...and the unnamed visible field shows the EU rendering.
    assert 'value="21/09/2026"' in r.text
    assert "dd/mm/yyyy" in r.text  # placeholder
    # The lang hint stays (it's what fixes Chrome without any JS).
    assert '<html lang="en-GB">' in r.text


def test_visible_date_field_is_not_submitted(client):
    """The EU text input must be unnamed, or it would reach the route and 400."""
    tc, _ = client
    r = tc.get("/offers")
    # Grab the text input's tag and assert it carries no name attribute.
    import re

    tag = re.search(r"<input type=\"text\" data-eudate-text[^>]*>", r.text)
    assert tag is not None and "name=" not in tag.group(0)


def test_eudate_script_is_served(client):
    """The enhancement is a vendored local asset (no CDN, per the golden rule)."""
    tc, _ = client
    assert "eudate.js" in tc.get("/offers").text
    r = tc.get("/static/eudate.js")
    assert r.status_code == 200 and "euToIso" in r.text


def test_detail_shows_exact_eu_date(client):
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute("UPDATE jobs SET first_seen_at=? WHERE id=?",
                 ("2026-09-21T17:29:39+00:00", ids["a"]))
    conn.commit()
    conn.close()
    r = tc.get(f"/offers/{ids['a']}")
    assert "21/09/2026" in r.text


def test_filters_survive_leaving_and_returning(client):
    """The whole point: filter, visit an offer, come back to a bare /offers."""
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute("UPDATE jobs SET source='linkedin_email' WHERE id=?", (ids["b"],))
    conn.commit()
    conn.close()

    # Filter to one source, then navigate away and back via the plain nav link.
    assert "BetaCorp" not in tc.get("/offers?source=wttj").text
    tc.get(f"/offers/{ids['a']}")
    back = tc.get("/offers")
    assert "BetaCorp" not in back.text          # the filter came back
    assert "Restored your last filters" in back.text


def test_explicit_url_overrides_saved_view(client):
    """A URL with filters always wins — shared links mean what they say."""
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute("UPDATE jobs SET source='linkedin_email' WHERE id=?", (ids["b"],))
    conn.commit()
    conn.close()

    tc.get("/offers?source=wttj")                      # saved
    r = tc.get("/offers?source=linkedin_email")        # explicit wins
    assert "BetaCorp" in r.text and "ACME" not in r.text
    assert "Restored your last filters" not in r.text
    # …and becomes the new saved view.
    assert "BetaCorp" in tc.get("/offers").text


def test_reset_clears_the_saved_view(client):
    """?reset=1 is the escape hatch: forget, and stay forgotten."""
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute("UPDATE jobs SET source='linkedin_email' WHERE id=?", (ids["b"],))
    conn.commit()
    conn.close()

    tc.get("/offers?source=wttj")
    r = tc.get("/offers?reset=1")
    assert "ACME" in r.text and "BetaCorp" in r.text     # defaults restored
    assert "BetaCorp" in tc.get("/offers").text          # and it stayed cleared


def test_no_saved_view_renders_defaults(client):
    """A first visit (nothing saved) is the plain default view, unannounced."""
    tc, _ = client
    r = tc.get("/offers")
    assert r.status_code == 200
    assert "Restored your last filters" not in r.text


def test_new_preset_is_not_persisted(client):
    """?new=1 is a one-shot shortcut, not a filter to replay forever."""
    tc, _ = client
    _seed_ingest_run(tc, "2099-01-01T00:00:00+00:00", {"stage": "ingest", "wttj": {"new": 0}})
    tc.get("/offers?new=1")                       # would show an empty batch
    r = tc.get("/offers")
    assert "ACME" in r.text                       # not stuck on the empty preset


def test_saved_view_survives_a_corrupt_state_file(client):
    """A damaged state file degrades to the default view, never a 500."""
    tc, _ = client
    tc.get("/offers?source=wttj")
    state = tc.app.state.settings.project_root / "data" / "view_state.json"
    state.write_text("{{{corrupt", encoding="utf-8")
    r = tc.get("/offers")
    assert r.status_code == 200 and "ACME" in r.text


def _set_status(tc, job_id, status):
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute("UPDATE jobs SET filter_status=? WHERE id=?", (status, job_id))
    conn.commit()
    conn.close()


def test_status_defaults_to_active_only(client):
    """No status param → passed + needs_review pre-checked, rejected hidden."""
    tc, ids = client
    _set_status(tc, ids["b"], "rejected")
    r = tc.get("/offers/table")
    assert r.status_code == 200
    assert "ACME" in r.text and "BetaCorp" not in r.text


def test_status_accepts_multiple(client):
    tc, ids = client
    _set_status(tc, ids["b"], "rejected")
    r = tc.get("/offers/table?filter_status=passed&filter_status=rejected")
    assert "ACME" in r.text and "BetaCorp" in r.text


def test_status_single_value(client):
    tc, ids = client
    _set_status(tc, ids["b"], "rejected")
    r = tc.get("/offers/table?filter_status=rejected")
    assert "BetaCorp" in r.text and "ACME" not in r.text


def test_status_cleared_means_all(client):
    """Unchecking every box (marker present, no values) shows everything.

    Distinguished from a first visit by the hidden marker param — without it,
    an empty selection would be indistinguishable from the default.
    """
    tc, ids = client
    _set_status(tc, ids["b"], "rejected")
    r = tc.get("/offers/table?filter_status_set=1")
    assert "ACME" in r.text and "BetaCorp" in r.text  # rejected no longer hidden


def test_status_unknown_value_is_400(client):
    tc, _ = client
    assert tc.get("/offers/table?filter_status=bogus").status_code == 400


def test_offers_table_multi_source(client):
    """A repeated query param selects several sources (OR within the filter)."""
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute("UPDATE jobs SET source='linkedin_email' WHERE id=?", (ids["b"],))
    conn.commit()
    conn.close()

    r = tc.get("/offers/table?source=wttj&source=linkedin_email")
    assert r.status_code == 200
    assert "ACME" in r.text and "BetaCorp" in r.text

    r = tc.get("/offers/table?source=wttj")
    assert "ACME" in r.text and "BetaCorp" not in r.text


def test_offers_table_multi_disposition_with_unreviewed(client):
    """"Unreviewed" combines with a real disposition rather than overriding it."""
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    db.upsert_review(conn, ids["a"], disposition="applied")
    conn.commit()
    conn.close()

    r = tc.get(f"/offers/table?disposition={queries.UNREVIEWED}&disposition=applied")
    assert r.status_code == 200
    assert "ACME" in r.text and "BetaCorp" in r.text  # applied + unreviewed

    r = tc.get("/offers/table?disposition=applied")
    assert "ACME" in r.text and "BetaCorp" not in r.text


def test_offers_table_empty_filter_means_all(client):
    """The old ""-submits-as-all behaviour still holds (no blank option now)."""
    tc, _ = client
    r = tc.get("/offers/table?source=")
    assert r.status_code == 200 and "ACME" in r.text and "BetaCorp" in r.text


def test_offers_table_unknown_filter_value_is_400(client):
    """An out-of-allowlist value is a UI/server mismatch — fail loudly."""
    tc, _ = client
    assert tc.get("/offers/table?source=nope").status_code == 400
    assert tc.get("/offers/table?score_status=nope").status_code == 400
    assert tc.get("/offers/table?disposition=nope").status_code == 400
    # A valid value alongside an invalid one is still rejected.
    assert tc.get("/offers/table?source=wttj&source=nope").status_code == 400


def test_offers_list_renders_checkbox_dropdowns(client):
    """All four filters render as checkbox dropdowns, not native selects."""
    tc, _ = client
    r = tc.get("/offers")
    assert r.status_code == 200
    assert r.text.count('class="ms-drop"') == 4  # status, source, score, review
    assert 'type="checkbox" name="source"' in r.text
    assert "<select name=\"source\"" not in r.text  # the old control is gone


def test_offers_list_summary_shows_selection(client):
    """A collapsed filter names what's chosen; empty reads as "All"."""
    tc, _ = client
    r = tc.get("/offers")
    # Source is unfiltered by default → its summary says "All".
    assert ">All<" in r.text.replace("\n", "")

    r = tc.get("/offers?source=wttj")
    assert "wttj" in r.text


def test_offers_table_bad_seen_after_is_400(client):
    """A malformed ingestion-date bound is a client error, not a silent no-match."""
    tc, _ = client
    r = tc.get("/offers/table?seen_after=not-a-date")
    assert r.status_code == 400


def test_offers_table_seen_after_filters(client):
    """seen_after drops offers ingested before the bound."""
    tc, _ = client
    # Both fixture offers are ingested "now", so a future bound must hide them.
    r = tc.get("/offers/table?seen_after=2099-01-01")
    assert r.status_code == 200 and "Product Manager" not in r.text
    r = tc.get("/offers/table?seen_after=2000-01-01")
    assert "Product Manager" in r.text


def _seed_ingest_run(tc, started_at, counts):
    """Write an ingest row into the ledger of the app's own DB."""
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, source_counts_json, dry_run) "
        "VALUES (?, ?, ?, 0)",
        (started_at, started_at, json.dumps(counts)),
    )
    conn.commit()
    conn.close()


def test_new_preset_selects_last_ingest_batch(client):
    """?new=1 shows the offers whose first_seen_at is at/after the last ingest."""
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    # 'a' belongs to the latest batch; 'b' was ingested long before it.
    conn.execute("UPDATE jobs SET first_seen_at=? WHERE id=?",
                 ("2026-08-10T12:00:00+00:00", ids["a"]))
    conn.execute("UPDATE jobs SET first_seen_at=? WHERE id=?",
                 ("2026-01-01T00:00:00+00:00", ids["b"]))
    conn.commit()
    conn.close()
    _seed_ingest_run(tc, "2026-08-10T11:59:00+00:00", {"stage": "ingest", "wttj": {"new": 1}})

    r = tc.get("/offers?new=1")
    assert r.status_code == 200
    assert "ACME" in r.text and "BetaCorp" not in r.text


def test_new_preset_clears_posted_after_default(client):
    """The 30-day posted default must not narrow the "what's new" view.

    A freshly-ingested but old-dated posting has to show up; otherwise the two
    date bounds intersect and the preset silently hides what it exists to show.
    """
    tc, ids = client
    conn = db.connect(tc.app.state.settings.db_path)
    conn.execute(
        "UPDATE jobs SET first_seen_at=?, posted_at=? WHERE id=?",
        ("2026-08-10T12:00:00+00:00", "2020-01-01T00:00:00+00:00", ids["a"]),
    )
    conn.commit()
    conn.close()
    _seed_ingest_run(tc, "2026-08-10T11:59:00+00:00", {"stage": "ingest", "wttj": {"new": 1}})

    assert "ACME" in tc.get("/offers?new=1").text


def test_new_preset_empty_batch_says_so(client):
    """An ingest that added nothing gets its own message, not "no match"."""
    tc, _ = client
    # A run newer than every offer's first_seen_at ⇒ the batch is empty.
    _seed_ingest_run(tc, "2099-01-01T00:00:00+00:00", {"stage": "ingest", "wttj": {"new": 0}})
    r = tc.get("/offers?new=1")
    assert r.status_code == 200 and "added no new offers" in r.text


def test_offers_list_shows_added_column_without_ingest_run(client):
    """No ingest row in the ledger ⇒ no shortcut, but the list still renders."""
    tc, _ = client
    r = tc.get("/offers")
    assert r.status_code == 200
    assert "Added" in r.text
    assert "New since last ingest" not in r.text


def test_offer_detail_renders_blocks(client):
    tc, ids = client
    r = tc.get(f"/offers/{ids['a']}")
    assert r.status_code == 200
    assert "Strong fit for a B2B SaaS PM." in r.text  # verdict
    assert "ACME builds AI workflow tools." in r.text  # company summary
    assert "salary not stated" in r.text               # red flag
    assert "View original posting" in r.text           # outbound link


def test_offer_detail_404(client):
    tc, _ = client
    r = tc.get("/offers/999999")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


# --- V2 write routes ------------------------------------------------------


def test_set_review_writes_and_returns_fragment(client):
    tc, ids = client
    r = tc.post(f"/offers/{ids['a']}/review", data={"disposition": "applied", "notes": "call"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()  # control fragment, not a full page
    assert "applied" in r.text
    # dashboard now counts it
    assert ">1<" in tc.get("/").text.replace(" ", "")


def test_set_review_bad_disposition_400(client):
    tc, ids = client
    r = tc.post(f"/offers/{ids['a']}/review", data={"disposition": "bogus"})
    assert r.status_code == 400


def test_set_review_missing_offer_404(client):
    tc, _ = client
    r = tc.post("/offers/999999/review", data={"disposition": "applied"})
    assert r.status_code == 404


def test_offers_table_disposition_filter(client):
    tc, ids = client
    tc.post(f"/offers/{ids['a']}/review", data={"disposition": "applied"})
    applied = tc.get("/offers/table?disposition=applied").text
    assert "Product Manager" in applied  # offer 'a' is a PM titled row
    unreviewed = tc.get("/offers/table?disposition=__unreviewed__").text
    # offer 'a' is now reviewed, so BetaCorp (b) is the unreviewed one
    assert "BetaCorp" in unreviewed


def test_run_status_and_trigger(client):
    tc, _ = client
    assert tc.get("/runs/status").status_code == 200
    # a fast, safe stage (commute-only recompute from stored score; no LLM/net)
    r = tc.post("/runs/score-commute-only")
    assert r.status_code == 200


def test_trigger_unknown_stage_404(client):
    tc, _ = client
    assert tc.post("/runs/nonsense").status_code == 404


def test_per_offer_run_routes(client):
    tc, ids = client
    # score is a per-offer stage; a missing offer 404s; an unknown stage 404s.
    assert tc.post(f"/offers/{ids['a']}/runs/score").status_code == 200
    assert tc.post("/offers/999999/runs/score").status_code == 404
    assert tc.post(f"/offers/{ids['a']}/runs/ingest").status_code == 404  # not per-offer
    assert tc.post(f"/offers/{ids['a']}/runs/nonsense").status_code == 404


def test_require_local_origin(client):
    tc, ids = client
    # A foreign Origin is refused; no Origin is allowed (default in TestClient).
    bad = tc.post(
        f"/offers/{ids['a']}/review",
        data={"disposition": "applied"},
        headers={"origin": "http://evil.example"},
    )
    assert bad.status_code == 403

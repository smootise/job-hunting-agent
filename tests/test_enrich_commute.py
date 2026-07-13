"""Tests for the commute-enrichment orchestration stage.

Fully offline: both the BAN geocoding client and the Google routing client are
injected httpx.MockTransport clients. Seeds a temp DB the way `jobscout filter`
would leave it (filter_status set), then drives run_enrich_commute and asserts on
the persisted rows + the summary.

Covers: remote → commute 0 & no routing, resolved → commute written, approximate
address counted, idempotency (second run considers nothing), --re-enrich,
--dry-run writes nothing, --limit, per-row fail-soft, only passed/needs_review
enriched (rejected skipped), and the home-coords-never-persisted guard.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobscout.models import JobRecord
from jobscout.pipeline import enrich_commute
from jobscout.storage import db

PREFS_YAML = """\
home:
  address: "1 rue Home, 78000 Versailles"
  lat: 48.80
  lon: 2.13
commute:
  min_bike_walk_minutes: 12
  max_bike_distance_km: 10
"""

ENV = {"GOOGLE_ROUTES_KEY": "test-key"}


@pytest.fixture
def prefs_path(tmp_path):
    p = tmp_path / "preferences.yaml"
    p.write_text(PREFS_YAML, encoding="utf-8")
    return p


@pytest.fixture
def env_path(tmp_path):
    p = tmp_path / ".env"
    p.write_text("GOOGLE_ROUTES_KEY=test-key\n", encoding="utf-8")
    return p


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "jobs.db"


def _job(external_id, *, title="Product Manager", location="Paris, Île-de-France",
         description="Poste hybride, 2 jours de télétravail.", source="wttj"):
    return JobRecord(
        source=source, external_id=external_id, url=f"http://x/{external_id}",
        title=title, company="Acme", location=location, contract_type="CDI",
        salary_text=None, description=description, posted_at=None, lang="fr",
    )


def _seed(db_path, jobs, *, status="passed"):
    conn = db.connect(db_path)
    db.upsert_jobs(conn, jobs)
    for j in jobs:
        row = conn.execute("SELECT id FROM jobs WHERE external_id=?", (j.external_id,)).fetchone()
        db.record_filter_verdict(conn, row["id"], filter_status=status, filter_reasons_json="[]")
    conn.commit()
    conn.close()


# --- mock clients --------------------------------------------------------


def _geo_client(city_payload=None):
    """BAN mock: returns a Paris feature for any query (or a custom payload)."""
    payload = city_payload or {
        "features": [{
            "geometry": {"coordinates": [2.35, 48.86]},
            "properties": {"label": "Paris", "city": "Paris", "postcode": "75002",
                           "context": "75, Paris, Île-de-France", "score": 0.9},
        }]
    }

    def handler(request):
        return httpx.Response(200, text=json.dumps(payload))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _routing_client(minutes_by_mode=None, fail=False):
    """Google Routes mock keyed on travelMode."""
    minutes_by_mode = minutes_by_mode or {"TRANSIT": 40, "BICYCLE": 30, "TRANSIT_RAIL": 45}

    def handler(request):
        if fail:
            return httpx.Response(500, text="{}")
        body = json.loads(request.content)
        mode = body["travelMode"]
        key = "TRANSIT_RAIL" if (mode == "TRANSIT" and "transitPreferences" in body) else mode
        mins = minutes_by_mode.get(key)
        if mins is None:
            return httpx.Response(200, text=json.dumps({"routes": []}))
        return httpx.Response(200, text=json.dumps(
            {"routes": [{"duration": f"{int(mins*60)}s", "distanceMeters": 5000,
                         "legs": [{"steps": [{"travelMode": "TRANSIT",
                                              "staticDuration": f"{int(mins*60)}s",
                                              "distanceMeters": 5000}]}]}]}))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _run(db_path, prefs_path, env_path, **kw):
    return enrich_commute.run_enrich_commute(
        prefs_path=prefs_path, env_path=env_path, db_path=db_path,
        _geo_client=kw.pop("geo", _geo_client()),
        _routing_client=kw.pop("routing", _routing_client()),
        **kw,
    )


# --- tests ---------------------------------------------------------------


def test_resolved_offer_gets_commute(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")])
    summary = _run(db_path, prefs_path, env_path)
    assert summary.enriched == 1 and summary.failed == 0

    conn = db.connect(db_path)
    row = conn.execute(
        "SELECT commute_minutes, commute_mode, commute_strategies, address_source, enriched_at "
        "FROM jobs WHERE external_id='1'").fetchone()
    assert row["commute_minutes"] == 30  # fastest of 40/30/hybrid
    assert row["commute_mode"] == "bike_only"
    assert row["enriched_at"] is not None
    strategies = json.loads(row["commute_strategies"])
    assert set(strategies["strategies"]) == {"no_bike", "bike_only", "bike_hybrid"}
    conn.close()


def test_remote_offer_commute_zero_no_routing(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1", location="Full remote",
                          description="100% télétravail, full remote.")])

    def boom(request):
        raise AssertionError("remote offers must not route")

    routing_client = httpx.Client(transport=httpx.MockTransport(boom))
    summary = _run(db_path, prefs_path, env_path, routing=routing_client)
    assert summary.remote_skipped == 1 and summary.enriched == 1

    conn = db.connect(db_path)
    row = conn.execute("SELECT commute_minutes, commute_mode FROM jobs WHERE external_id='1'").fetchone()
    assert row["commute_minutes"] == 0.0 and row["commute_mode"] == "remote"
    conn.close()


def test_approximate_address_counted(db_path, prefs_path, env_path):
    # Out-of-IDF city → approximate/low.
    lyon = {"features": [{"geometry": {"coordinates": [4.83, 45.76]},
            "properties": {"label": "Lyon", "city": "Lyon", "postcode": "69002",
                           "context": "69, Rhône, ARA", "score": 0.9}}]}
    _seed(db_path, [_job("1", location="Lyon", description="Sur site.")])
    summary = _run(db_path, prefs_path, env_path, geo=_geo_client(lyon))
    assert summary.enriched == 1 and summary.approximate == 1


def test_idempotent_second_run(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")])
    _run(db_path, prefs_path, env_path)
    second = _run(db_path, prefs_path, env_path)
    assert second.considered == 0 and second.enriched == 0


def test_re_enrich_reprocesses(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")])
    _run(db_path, prefs_path, env_path)
    again = _run(db_path, prefs_path, env_path, re_enrich=True)
    assert again.considered == 1


def test_dry_run_writes_nothing(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")])
    summary = _run(db_path, prefs_path, env_path, dry_run=True)
    assert summary.enriched == 1
    conn = db.connect(db_path)
    row = conn.execute("SELECT commute_minutes, enriched_at FROM jobs WHERE external_id='1'").fetchone()
    assert row["commute_minutes"] is None and row["enriched_at"] is None
    conn.close()


def test_limit_caps_rows(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1"), _job("2"), _job("3")])
    summary = _run(db_path, prefs_path, env_path, limit=2)
    assert summary.considered == 2


def test_rejected_offers_not_enriched(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")], status="rejected")
    summary = _run(db_path, prefs_path, env_path)
    assert summary.considered == 0


def test_needs_review_offers_are_enriched(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")], status="needs_review")
    summary = _run(db_path, prefs_path, env_path)
    assert summary.considered == 1 and summary.enriched == 1


def test_routing_failure_is_failsoft(db_path, prefs_path, env_path):
    _seed(db_path, [_job("1")])
    summary = _run(db_path, prefs_path, env_path, routing=_routing_client(fail=True))
    assert summary.failed == 1 and summary.enriched == 0
    conn = db.connect(db_path)
    row = conn.execute("SELECT commute_minutes FROM jobs WHERE external_id='1'").fetchone()
    assert row["commute_minutes"] is None  # untouched; a later run can retry
    conn.close()


def test_home_coords_never_persisted(db_path, prefs_path, env_path):
    # The home coords (48.80, 2.13) must appear on NO job row.
    _seed(db_path, [_job("1")])
    _run(db_path, prefs_path, env_path)
    conn = db.connect(db_path)
    row = conn.execute("SELECT lat, lon FROM jobs WHERE external_id='1'").fetchone()
    # The stored lat/lon are the OFFICE (Paris 48.86/2.35), never home 48.80/2.13.
    assert row["lat"] != 48.80 and row["lon"] != 2.13
    conn.close()

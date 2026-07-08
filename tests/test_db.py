"""Tests for schema init, the run ledger, and the dry-run guarantee."""

from __future__ import annotations

import sqlite3

from jobscout.models import JobRecord
from jobscout.pipeline import ingest
from jobscout.storage import db


def test_init_schema_is_idempotent():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    db.init_schema(c)  # second call must not raise
    tables = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "runs"} <= tables
    c.close()


def test_connect_creates_db_file(tmp_path):
    path = tmp_path / "sub" / "jobs.db"
    conn = db.connect(path)
    assert path.exists()  # parent dir created too
    conn.close()


def test_run_ledger_roundtrip():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)

    run_id = db.record_run_start(c, dry_run=False)
    db.record_run_finish(c, run_id, {"wttj": {"new": 3}})

    row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert "wttj" in row["source_counts_json"]
    c.close()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """--dry-run must not create or modify the DB file."""
    db_path = tmp_path / "jobs.db"

    # Stub one source to return a record without hitting the network.
    def fake_fetch(*, limit):
        return [JobRecord(
            source="wttj", external_id="x1", url="https://x/1",
            title="Product Manager", company="Acme", location="Paris",
            contract_type="CDI", salary_text=None, description=None,
            posted_at=None, lang="fr",
        )]

    monkeypatch.setitem(ingest.SOURCES, "wttj", fake_fetch)

    summary = ingest.run_ingest(
        sources=["wttj"], limit=10, dry_run=True, db_path=db_path
    )

    assert summary.dry_run is True
    assert summary.per_source["wttj"].fetched == 1
    assert not db_path.exists()  # nothing written in dry-run


def test_real_run_persists_and_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"

    def fake_fetch(*, limit):
        return [JobRecord(
            source="wttj", external_id="x1", url="https://x/1",
            title="Product Manager", company="Acme", location="Paris",
            contract_type="CDI", salary_text=None, description=None,
            posted_at=None, lang="fr",
        )]

    monkeypatch.setitem(ingest.SOURCES, "wttj", fake_fetch)

    first = ingest.run_ingest(sources=["wttj"], dry_run=False, db_path=db_path)
    assert first.per_source["wttj"].new == 1

    second = ingest.run_ingest(sources=["wttj"], dry_run=False, db_path=db_path)
    assert second.per_source["wttj"].new == 0
    assert second.per_source["wttj"].seen_again == 1


def test_failing_source_is_soft(tmp_path, monkeypatch):
    """One source raising must not sink the run; it's reported as failed."""
    db_path = tmp_path / "jobs.db"

    def boom(*, limit):
        raise RuntimeError("api down")

    monkeypatch.setitem(ingest.SOURCES, "wttj", boom)

    summary = ingest.run_ingest(sources=["wttj"], dry_run=False, db_path=db_path)
    assert summary.per_source["wttj"].failed is True
    assert "api down" in summary.per_source["wttj"].error

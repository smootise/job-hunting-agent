"""Fail-fast smoke test for the webapp: drives the key routes end-to-end.

Runs the real FastAPI app in-process via ``TestClient`` (so there is NO server to
launch, orphan, or clean up — it can't leave a process holding the port / the
jobscout.exe lock). Exercises the read views, a review write, and a per-offer run
trigger against a **throwaway temp DB** seeded with one offer, so it never touches
your real ``data/jobs.db``. A green run means the routes, templates, review write
path, and the background runner all wire together.

Usage:  uv run python scripts/webapp_smoke.py

This is throwaway verification tooling — not part of the jobscout package — but it
imports the same ``jobscout.web`` code the real server runs.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from jobscout.models import JobRecord  # noqa: E402
from jobscout.storage import db  # noqa: E402
from jobscout.web.app import create_app  # noqa: E402
from jobscout.web.settings import Settings  # noqa: E402

# Prefer the committed example prefs so criteria render without the gitignored
# real preferences.yaml; fall back to the real one if the example is absent.
_PREFS = REPO_ROOT / "preferences.example.yaml"
if not _PREFS.exists():
    _PREFS = REPO_ROOT / "preferences.yaml"

_checks = 0
_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    mark = "[OK]" if ok else "[FAIL]"
    print(f"  {mark} {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _seed(db_path: Path) -> int:
    conn = db.connect(db_path)
    job = JobRecord(
        source="wttj", external_id="smoke-1",
        url="https://example.test/job/1", title="Product Manager",
        company="SmokeCorp", location="Paris", contract_type="CDI",
        salary_text=None, description="A PM role for the smoke test.",
        posted_at=None, lang="en",
    )
    db.upsert_jobs(conn, [job])
    jid = conn.execute("SELECT id FROM jobs WHERE external_id='smoke-1'").fetchone()["id"]
    db.record_filter_verdict(conn, jid, filter_status="passed", filter_reasons_json="[]")
    db.record_score(
        conn, jid, score_total=77.0, score_status="scored",
        score_json=json.dumps({
            "reasoning": "Smoke-test verdict.", "red_flags": [],
            "criteria_scores": {"role_scope_and_focus": 8},
            "weekly_commute_fit": 6.0, "onsite_days": 3,
            "remote_policy": "hybrid", "commute_included_in_total": True,
        }),
    )
    conn.commit()
    conn.close()
    return jid


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "jobs.db"
    jid = _seed(db_path)
    settings = Settings(
        project_root=tmp, db_path=db_path, preferences_path=_PREFS,
        env_path=tmp / ".env",
    )

    print("Driving the webapp routes in-process (temp DB, no real server)...\n")
    # Context-manager form runs the lifespan → app.state.runner exists.
    with TestClient(create_app(settings)) as tc:
        check("GET /healthz", tc.get("/healthz").status_code == 200)
        check("GET / (dashboard)", tc.get("/").status_code == 200)

        r = tc.get("/offers")
        check("GET /offers", r.status_code == 200 and "SmokeCorp" in r.text)

        r = tc.get("/offers/table?sort=score_total&dir=desc")
        check("GET /offers/table (sorted fragment)",
              r.status_code == 200 and "<table" in r.text and "<html" not in r.text.lower())

        r = tc.get(f"/offers/{jid}")
        check(f"GET /offers/{jid} (detail)",
              r.status_code == 200 and "Smoke-test verdict." in r.text)

        # Write: set a review disposition (same-origin — TestClient sends no Origin).
        r = tc.post(f"/offers/{jid}/review", data={"disposition": "applied", "notes": "smoke"})
        check("POST review (applied)", r.status_code == 200 and "applied" in r.text)
        # It landed in the dashboard counts.
        check("dashboard reflects the review", ">1<" in tc.get("/").text.replace(" ", ""))
        # Disposition filter works.
        check("filter disposition=applied",
              "SmokeCorp" in tc.get("/offers/table?disposition=applied").text)

        # Per-offer run trigger: a fast, safe stage (commute-only, no LLM/network).
        r = tc.post(f"/offers/{jid}/runs/score-commute-only")
        check("POST per-offer run (score-commute-only)", r.status_code == 200)
        # Let the background worker finish, then confirm status renders.
        time.sleep(0.5)
        check("GET /runs/status", tc.get("/runs/status").status_code == 200)

        # Cancel is a same-origin no-op when idle; returns the status fragment.
        check("POST /runs/cancel (idle no-op)",
              tc.post("/runs/cancel").status_code == 200)

        # Guards.
        check("unknown per-offer stage -> 404",
              tc.post(f"/offers/{jid}/runs/nonsense").status_code == 404)
        check("missing offer detail -> 404", tc.get("/offers/999999").status_code == 404)
        check("cross-origin write refused",
              tc.post(f"/offers/{jid}/review", data={"disposition": "to_review"},
                      headers={"origin": "http://evil.test"}).status_code == 403)

    print()
    if _failures:
        raise SystemExit(f"[FAIL] {len(_failures)}/{_checks} checks failed: {_failures}")
    print(f"[OK] All {_checks} webapp smoke checks passed.")


if __name__ == "__main__":
    main()

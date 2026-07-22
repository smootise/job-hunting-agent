"""Fail-fast smoke test for SearXNG + the company-research agent wiring.

Run this ONCE after standing up SearXNG (SEARXNG_URL in .env) and before the
first real `jobscout research-company`. It runs the *real* agent loop + the
grounding-verification pass on a synthetic offer, so a search-unreachable or
Ollama-down problem surfaces here with a clear message. Throwaway validation
tooling — not part of the jobscout package — but it uses the same
`jobscout.agents.company_agent` code the stage uses.

Usage:  uv run python scripts/research_company_smoke.py ["Company Name"]

Runs one WTTJ-shaped offer (so the deterministic profile fetch is exercised).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from jobscout import config  # noqa: E402
from jobscout.agents import company_agent, tools  # noqa: E402
from jobscout.models import JobRecord  # noqa: E402
from jobscout.pipeline import research_company as rc  # noqa: E402

DEFAULT_COMPANY = "Doctolib"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    company = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COMPANY

    try:
        searxng = config.searxng_url()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    print(f"SearXNG: {searxng}\n")

    # A WTTJ-shaped offer so the deterministic profile fetch is exercised. The
    # slug is best-effort; a 404 just means the agent works from search alone.
    slug = company.lower().replace(" ", "-")
    record = JobRecord(
        source="wttj", external_id="smoke",
        url=f"https://www.welcometothejungle.com/fr/companies/{slug}/jobs/product-manager",
        title="Product Manager", company=company, location="Paris",
        contract_type="CDI", salary_text=None, description="A PM role.",
        posted_at=None, lang="fr",
    )

    search_client = httpx.Client(timeout=tools.SEARCH_TIMEOUT)
    fetch_client = httpx.Client(timeout=tools.FETCH_TIMEOUT, follow_redirects=True)
    cache = tools.FetchCache()

    # Exercise the REAL stage helpers (not a hand-rolled copy) so this smoke test
    # can't drift from what `jobscout research-company` actually does — that drift
    # is exactly what hid a grounding bug before.
    evidence = rc._Evidence()
    tools_map = rc._build_tools(searxng, search_client, fetch_client, cache, evidence)

    print("Step 1: deterministic WTTJ company profile (organizations index)...")
    wttj_src, wttj_text = company_agent.fetch_wttj_profile(record, client=fetch_client)
    if wttj_text:
        print(f"  source: {wttj_src}")
        print("  profile:\n    " + wttj_text.replace("\n", "\n    ") + "\n")
    else:
        print("  none (agent works from search)\n")

    print("Step 2-3: agent drafts the brief (calls the local model)...")
    draft = company_agent.research_company(record, tools_map=tools_map, wttj_text=wttj_text)
    if draft is None:
        raise SystemExit("[FAIL] Agent produced no draft (model/loop issue).")
    print(f"  draft: {json.dumps(draft, ensure_ascii=False)[:300]}\n")

    print("Step 4: grounding-verification pass...")
    sources = rc._collect_sources(record, wttj_text, evidence)
    print(f"  grounding against {len(sources)} source text(s) "
          f"({len(evidence.pages)} fetched pages, {len(evidence.snippets)} snippets, "
          f"+ description/WTTJ)")
    brief = company_agent.verify_brief(draft, sources)

    print(f"\n  grounded brief:\n{json.dumps(brief.as_json_dict(), ensure_ascii=False, indent=2)}")
    if brief.needs_review:
        print("\n[OK] Wiring works. Grounding stripped the brief to nothing "
              "(needs_review) — a clean, honest outcome. Try a better-known "
              "company for a positive brief.")
    else:
        print("\n[OK] SearXNG + agent + grounding all work. "
              "Safe to run `jobscout research-company`.")


if __name__ == "__main__":
    main()

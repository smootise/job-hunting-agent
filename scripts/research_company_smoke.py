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

    def web_search(query: str):
        return tools.web_search(query, searxng_url=searxng, client=search_client)

    def fetch_page(url: str):
        return tools.fetch_page(url, policy=tools.POLICY_SOFT, client=fetch_client, cache=cache)

    tools_map = {"web_search": web_search, "fetch_page": fetch_page}

    def _fetch(url, *, policy, cache):
        return tools.fetch_page(url, policy=policy, client=fetch_client, cache=cache)

    print("Step 1: deterministic WTTJ profile fetch...")
    wttj_url, wttj_text = company_agent.fetch_wttj_profile(record, fetch=_fetch, cache=cache)
    print(f"  profile url: {wttj_url}")
    print(f"  profile text: {'captured (' + str(len(wttj_text)) + ' chars)' if wttj_text else 'none (agent works from search)'}\n")

    print("Step 2-3: agent drafts the brief (calls the local model)...")
    draft = company_agent.research_company(record, tools_map=tools_map, wttj_text=wttj_text)
    if draft is None:
        raise SystemExit("[FAIL] Agent produced no draft (model/loop issue).")
    print(f"  draft: {json.dumps(draft, ensure_ascii=False)[:300]}\n")

    print("Step 4: grounding-verification pass...")
    sources = [t for t in (wttj_text,) if t]
    for url in (draft.get("sources") or []):
        c = cache.get(url) if isinstance(url, str) else None
        if c:
            sources.append(c)
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

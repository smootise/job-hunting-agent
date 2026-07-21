"""Fail-fast smoke test for SearXNG + the address-research agent wiring.

Run this ONCE after standing up SearXNG (and setting SEARXNG_URL in .env),
before the first real `jobscout research-address`, so a search-unreachable or
Ollama-down problem surfaces here with a clear message instead of deep inside the
pipeline. It runs the *real* agent loop (SearXNG + fetch_page + the local model)
on a known company, then the deterministic validation net, and prints the
outcome. Throwaway validation tooling — not part of the jobscout package — but it
routes through the same `jobscout.agents` code the stage uses, so a green run
means the wiring works.

Usage:  uv run python scripts/research_address_smoke.py ["Company Name"]

Defaults to a well-known Paris company if no name is given.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from jobscout import config  # noqa: E402
from jobscout.agents import address_agent, tools  # noqa: E402

DEFAULT_COMPANY = "Doctolib"  # a large, well-documented Paris/IDF company.


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    company = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COMPANY

    try:
        searxng = config.searxng_url()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    print(f"SearXNG: {searxng}")

    # Quick reachability check on SearXNG before spending an agent run.
    hits = tools.web_search(f'"{company}" Paris adresse', searxng_url=searxng)
    if not hits:
        raise SystemExit(
            "[FAIL] web_search returned nothing. Check SearXNG is up at "
            f"{searxng} and that `format=json` is enabled (see docs/agents.md)."
        )
    print(f"  web_search OK — {len(hits)} results; first: {hits[0]['url']}\n")

    search_client = httpx.Client(timeout=tools.SEARCH_TIMEOUT)
    fetch_client = httpx.Client(timeout=tools.FETCH_TIMEOUT, follow_redirects=True)
    cache = tools.FetchCache()

    def web_search(query: str):
        return tools.web_search(query, searxng_url=searxng, client=search_client)

    def fetch_page(url: str):
        return tools.fetch_page(url, policy=tools.POLICY_SOFT, client=fetch_client, cache=cache)

    tools_map = {"web_search": web_search, "fetch_page": fetch_page}

    print(f"Running the address agent for {company!r} (this calls the local model)...\n")
    candidate = address_agent.research_address(company, tools_map=tools_map)
    if candidate is None:
        raise SystemExit("[FAIL] Agent produced no final answer (model/loop issue).")

    print(f"  agent candidate: address={candidate.address!r} "
          f"confidence={candidate.confidence} evidence={candidate.evidence_url}")

    validated = address_agent.validate_candidate(candidate)
    if validated is None:
        print("\n[OK] Wiring works. The agent ran and the validation net rejected "
              "the candidate (no valid IDF address) — a clean fallback outcome. "
              "Try another company to see a positive resolution.")
        return

    print(f"  validated (IDF): {validated.address} "
          f"[{validated.confidence}] ({validated.lat:.4f}, {validated.lon:.4f})")
    print("\n[OK] SearXNG + agent + validation all work. "
          "Safe to run `jobscout research-address`.")


if __name__ == "__main__":
    main()

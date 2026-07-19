"""Address-research stage — the agent that sharpens the unroutable tail.

Runs *before* ``enrich-commute`` (which stays the sole router): for each offer
the deterministic chain can't place (``resolve_address`` → ``needs_address`` /
``unresolved``), invoke the address-research agent, validate its candidate
deterministically (BAN geocode + Île-de-France), and — on success — store a real
office address with ``address_source='agent'`` (no commute; routing is
``enrich-commute``'s job). Follows the established stage skeleton
(``enrich_commute.py`` / ``score_stage.py``):

  * **Idempotent by DB state.** Default scope = passed/needs_review rows not
    already placed by the agent; ``--redo`` re-offers all. Within scope, the
    "does this row even need the agent?" decision is ``resolve_address`` at run
    time (the single authority on address quality), NOT SQL.
  * **Fail-soft per row.** A search failure, a model error, or a non-geocoding
    candidate leaves the row untouched (the centroid fallback in
    ``enrich-commute`` still covers it). Never a crash.
  * **Injectable everything.** The generate fn, the tools, and the geocode HTTP
    client are parameters, so tests run fully offline.

SECURITY (CLAUDE.md): the agent has only read-only ``web_search`` + ``fetch_page``
(no write tools, no profile/home access); the candidate is validated
deterministically outside the agent; a wrong address can never hard-reject an
offer. No home coordinates are touched here (routing is downstream).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from jobscout import config
from jobscout.agents import address_agent, tools
from jobscout.enrich import address as address_mod
from jobscout.llm import client as llm_client
from jobscout.models import JobRecord
from jobscout.storage import db

logger = logging.getLogger("jobscout.research_address")

DEFAULT_MODEL = address_agent.loop.DEFAULT_MODEL


@dataclass
class ResearchAddressSummary:
    """The run's tally, rendered by the CLI."""

    dry_run: bool = False
    redo: bool = False
    considered: int = 0     # candidate rows examined
    needed_agent: int = 0   # of those, ones the deterministic chain couldn't place
    resolved: int = 0       # agent found an address that passed validation
    unresolved: int = 0     # agent found nothing usable (left for centroid fallback)


def run_research_address(
    *,
    prefs_path=config.DEFAULT_PREFERENCES_PATH,
    env_path=config.DEFAULT_ENV_PATH,
    db_path=db.DEFAULT_DB_PATH,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    redo: bool = False,
    dry_run: bool = False,
    _generate: address_agent.loop.GenerateFn | None = None,
    _search_client: httpx.Client | None = None,
    _fetch_client: httpx.Client | None = None,
    _geo_client: httpx.Client | None = None,
) -> ResearchAddressSummary:
    """Run the address agent over the unroutable tail; persist agent addresses.

    Resolves the SearXNG URL once, builds the two whitelisted tools bound to the
    per-run fetch cache + search client, then per candidate row: run
    ``resolve_address``; if it can't place the row, call the agent, validate, and
    store. ``dry_run`` researches + validates but writes nothing (LLM logs still
    land). Returns a summary the CLI renders.
    """
    env = config.load_env(env_path)
    generate = _generate or llm_client.generate
    summary = ResearchAddressSummary(dry_run=dry_run, redo=redo)

    conn = db.connect(db_path)
    run_id = None if dry_run else db.record_run_start(conn, dry_run=dry_run)
    owns_search = _search_client is None
    owns_fetch = _fetch_client is None
    owns_geo = _geo_client is None
    search_client = _search_client or httpx.Client(timeout=tools.SEARCH_TIMEOUT)
    fetch_client = _fetch_client or httpx.Client(timeout=tools.FETCH_TIMEOUT, follow_redirects=True)
    geo_client = _geo_client or httpx.Client(timeout=15.0)

    try:
        searxng = config.searxng_url(env)  # fail loud if unset — no agent without search.
        tools_map = _build_tools(searxng, search_client, fetch_client)

        rows = db.select_jobs_to_research_address(conn, limit=limit, redo=redo)
        summary.considered = len(rows)
        for row in rows:
            _research_row(
                conn, row, tools_map=tools_map, model=model, generate=generate,
                geo_client=geo_client, dry_run=dry_run, summary=summary,
            )
        if not dry_run:
            conn.commit()
    finally:
        if owns_search:
            search_client.close()
        if owns_fetch:
            fetch_client.close()
        if owns_geo:
            geo_client.close()
        if run_id is not None:
            db.record_run_finish(conn, run_id, {
                "stage": "research_address",
                "considered": summary.considered,
                "needed_agent": summary.needed_agent,
                "resolved": summary.resolved,
                "unresolved": summary.unresolved,
            })
        conn.close()

    return summary


def _build_tools(searxng: str, search_client, fetch_client) -> dict:
    """Bind the two whitelisted tools with their clients + a shared fetch cache.

    Closures adapt the general-purpose ``tools`` functions to the loop's
    ``tool(**args)`` calling convention: the loop passes only the model-chosen
    args (``query`` / ``url``); the clients, SearXNG URL, and cache are bound here
    and never exposed to the model.
    """
    cache = tools.FetchCache()

    def web_search(query: str) -> list[dict]:
        return tools.web_search(query, searxng_url=searxng, client=search_client)

    def fetch_page(url: str) -> str:
        return tools.fetch_page(
            url, policy=tools.POLICY_SOFT, client=fetch_client, cache=cache
        )

    return {"web_search": web_search, "fetch_page": fetch_page}


def _research_row(
    conn, row, *, tools_map, model, generate, geo_client, dry_run, summary,
) -> None:
    """Research one row's address, fail-soft. Only touches the unroutable tail."""
    try:
        record = JobRecord.from_row(row)
        resolved = address_mod.resolve_address(record, client=geo_client)

        # The deterministic chain already places most rows — the agent is only for
        # the tail it flags needs_address / unresolved. Everything else is skipped.
        if not (resolved.needs_address or resolved.source == "unresolved"):
            return

        summary.needed_agent += 1
        candidate = address_agent.research_address(
            record.company, tools_map=tools_map, model=model, generate=generate,
        )
        validated = address_agent.validate_candidate(candidate, client=geo_client)

        if validated is None:
            summary.unresolved += 1
            logger.info(
                "[%s] %r @ %r — agent found no valid IDF address (centroid fallback stands)",
                row["source"], row["title"], row["company"],
            )
            return

        summary.resolved += 1
        logger.info(
            "[%s] %r @ %r — agent resolved address (%s)",
            row["source"], row["title"], row["company"], validated.confidence,
        )
        if not dry_run:
            db.record_agent_address(
                conn, row["id"], address=validated.address, lat=validated.lat,
                lon=validated.lon, address_confidence=validated.confidence,
            )
    except Exception as exc:  # noqa: BLE001 — per-row fail-soft is the point.
        summary.unresolved += 1
        logger.warning(
            "research-address error [%s] %r: %s",
            row["source"], row["title"], type(exc).__name__,
        )

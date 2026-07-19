"""Company-research stage — a grounded company brief for every offer.

Runs on every passed/needs_review offer (before scoring): fetch the WTTJ profile
deterministically, let the agent fill gaps from the web, draft a brief, then
ground it against the fetched sources, and persist. Advisory context for the
scorer — never a hard gate. Follows the established stage skeleton
(``enrich_commute.py`` / ``score_stage.py``):

  * **Idempotent by DB state.** Default = passed/needs_review AND
    ``company_researched_at IS NULL``; ``--redo`` refreshes all. Every row is
    stamped after processing (even a ``needs_review`` brief) so it isn't retried
    every run.
  * **Fail-soft per row.** A search/fetch/model error yields a ``needs_review``
    brief (or none) and the batch continues — never a crash.
  * **Injectable everything.** generate fn + HTTP clients are parameters, so tests
    run fully offline.

SECURITY (CLAUDE.md): the agent has only read-only ``web_search`` + ``fetch_page``
(no write tools, no profile/home access); all fetched text is fenced untrusted by
the loop and again by the grounding pass; the brief reaches a ZERO-TOOL scorer as
labeled advisory context. A bad/injected brief can at worst nudge a score, caught
at human review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from jobscout import config
from jobscout.agents import company_agent, tools
from jobscout.llm import client as llm_client
from jobscout.models import JobRecord
from jobscout.storage import db

logger = logging.getLogger("jobscout.research_company")

DEFAULT_MODEL = company_agent.loop.DEFAULT_MODEL


@dataclass
class ResearchCompanySummary:
    """The run's tally, rendered by the CLI."""

    dry_run: bool = False
    redo: bool = False
    considered: int = 0      # rows selected for research
    briefed: int = 0         # rows we stored a usable (grounded) brief for
    needs_review: int = 0    # brief empty after grounding / agent gave nothing
    from_wttj: int = 0       # of considered, those with a deterministic WTTJ profile


def run_research_company(
    *,
    prefs_path=config.DEFAULT_PREFERENCES_PATH,
    env_path=config.DEFAULT_ENV_PATH,
    db_path=db.DEFAULT_DB_PATH,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    redo: bool = False,
    dry_run: bool = False,
    _generate: company_agent.loop.GenerateFn | None = None,
    _search_client: httpx.Client | None = None,
    _fetch_client: httpx.Client | None = None,
) -> ResearchCompanySummary:
    """Research every passed/needs_review offer's company; persist grounded briefs.

    Resolves SearXNG once, builds the whitelisted tools bound to a shared per-run
    fetch cache, then per row: deterministic WTTJ profile → agent draft → grounding
    pass → store. ``dry_run`` researches but writes nothing (LLM logs still land).
    """
    env = config.load_env(env_path)
    generate = _generate or llm_client.generate
    summary = ResearchCompanySummary(dry_run=dry_run, redo=redo)

    conn = db.connect(db_path)
    run_id = None if dry_run else db.record_run_start(conn, dry_run=dry_run)
    owns_search = _search_client is None
    owns_fetch = _fetch_client is None
    search_client = _search_client or httpx.Client(timeout=tools.SEARCH_TIMEOUT)
    fetch_client = _fetch_client or httpx.Client(timeout=tools.FETCH_TIMEOUT, follow_redirects=True)

    try:
        searxng = config.searxng_url(env)  # fail loud if unset.
        cache = tools.FetchCache()  # shared across rows: same company page fetched once.

        rows = db.select_jobs_to_research_company(conn, limit=limit, redo=redo)
        summary.considered = len(rows)
        for row in rows:
            _research_row(
                conn, row, searxng=searxng, search_client=search_client,
                fetch_client=fetch_client, cache=cache, model=model,
                generate=generate, dry_run=dry_run, summary=summary,
            )
        if not dry_run:
            conn.commit()
    finally:
        if owns_search:
            search_client.close()
        if owns_fetch:
            fetch_client.close()
        if run_id is not None:
            db.record_run_finish(conn, run_id, {
                "stage": "research_company",
                "considered": summary.considered,
                "briefed": summary.briefed,
                "needs_review": summary.needs_review,
                "from_wttj": summary.from_wttj,
            })
        conn.close()

    return summary


def _build_tools(searxng, search_client, fetch_client, cache, snippets) -> dict:
    """Bind the whitelisted tools for one row, capturing search snippets.

    The ``web_search`` wrapper records every result's snippet into ``snippets``
    so the grounding pass can use them as evidence even when the agent never
    fetches a page (the failure mode the Doctolib smoke run exposed: it searched
    three times, fetched nothing, and grounding then had no text to check). The
    fetch cache is shared per run; snippets are collected fresh per row.
    """
    def web_search(query: str) -> list[dict]:
        results = tools.web_search(query, searxng_url=searxng, client=search_client)
        for r in results:
            snippet = (r.get("snippet") or "").strip()
            if snippet:
                snippets.append(snippet)
        return results

    def fetch_page(url: str) -> str:
        return tools.fetch_page(
            url, policy=tools.POLICY_SOFT, client=fetch_client, cache=cache
        )

    return {"web_search": web_search, "fetch_page": fetch_page}


def _research_row(
    conn, row, *, searxng, search_client, fetch_client, cache, model, generate, dry_run, summary,
) -> None:
    """Research one company, fail-soft. Draft → ground → persist."""
    try:
        record = JobRecord.from_row(row)
        snippets: list[str] = []
        tools_map = _build_tools(searxng, search_client, fetch_client, cache, snippets)

        # Step 1 — deterministic WTTJ profile (highest signal, no LLM loop).
        # ``fetch_wttj_profile`` calls ``fetch(url, policy=…, cache=…)``; bind the
        # client here so it hits the network with the shared per-run cache.
        def _fetch(url, *, policy, cache):
            return tools.fetch_page(url, policy=policy, client=fetch_client, cache=cache)
        wttj_url, wttj_text = company_agent.fetch_wttj_profile(record, fetch=_fetch, cache=cache)
        if wttj_text:
            summary.from_wttj += 1

        # Step 2-3 — agent fills gaps and drafts the brief.
        draft = company_agent.research_company(
            record, tools_map=tools_map, wttj_text=wttj_text, model=model, generate=generate,
        )

        # Step 4 — ground the draft against ALL evidence we have: the job
        # description (we almost always have it), the WTTJ profile (when the fetch
        # succeeded), fetched pages, and search snippets. A missing WTTJ profile
        # never sinks the brief on its own — only a total absence of evidence does.
        source_texts = _collect_sources(record, wttj_text, draft, cache, snippets)
        brief = company_agent.verify_brief(
            draft, source_texts, model=model, generate=generate,
        )

        if brief.needs_review:
            summary.needs_review += 1
            logger.info("[%s] %r @ %r — brief needs_review (nothing grounded)",
                        row["source"], row["title"], row["company"])
        else:
            summary.briefed += 1
            logger.info("[%s] %r @ %r — brief stored (%s)",
                        row["source"], row["title"], row["company"], brief.confidence)

        if not dry_run:
            db.record_company_brief(
                conn, row["id"],
                company_brief_json=json.dumps(brief.as_json_dict(), ensure_ascii=False),
            )
    except Exception as exc:  # noqa: BLE001 — per-row fail-soft is the point.
        summary.needs_review += 1
        logger.warning("research-company error [%s] %r: %s",
                       row["source"], row["title"], type(exc).__name__)
        if not dry_run:
            # Still stamp so we don't retry a hard-failing row every run (--redo will).
            db.record_company_brief(conn, row["id"], company_brief_json=None)


def _collect_sources(record, wttj_text, draft, cache, snippets) -> list[str]:
    """Assemble the full evidence set the grounding pass checks the draft against.

    Four kinds of evidence, most-trusted first — the point (owner's decision) is
    that the brief grounds against *whatever we have*, so a failed WTTJ fetch
    never on its own leaves the fact-checker with nothing:

      1. **The job description** — untrusted posting text (fenced by the grounding
         prompt like every other source), but we almost always have it. This is
         the evidence that guarantees a missing WTTJ profile doesn't sink the brief.
      2. **The WTTJ profile** — when the deterministic fetch succeeded.
      3. **Fetched pages** the agent read (from the shared cache), preferring the
         URLs the draft cited to keep the prompt bounded.
      4. **Search snippets** the agent's ``web_search`` calls returned — the
         fallback for a run that searched but never fetched a page.

    ``verify_brief`` returns ``needs_review`` only when this whole set is empty.
    """
    texts: list[str] = []
    description = (record.description or "").strip()
    if description:
        texts.append(description)
    if wttj_text:
        texts.append(wttj_text)
    # Pages the agent fetched this run live in the cache; prefer the draft's cited
    # URLs (focused + bounded), fall back to nothing rather than dumping the cache.
    cited = (draft or {}).get("sources") or []
    if isinstance(cited, list):
        for url in cited:
            if isinstance(url, str):
                cached = cache.get(url)
                if cached and cached not in texts:
                    texts.append(cached)
    # Search snippets: short, but real evidence when no page was fetched.
    for snippet in snippets:
        if snippet and snippet not in texts:
            texts.append(snippet)
    return texts

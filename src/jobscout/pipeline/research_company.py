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
from dataclasses import dataclass, field

import httpx

from jobscout import config
from jobscout.agents import company_agent, tools
from jobscout.llm import client as llm_client
from jobscout.models import JobRecord
from jobscout.storage import db

logger = logging.getLogger("jobscout.research_company")

DEFAULT_MODEL = company_agent.loop.DEFAULT_MODEL


@dataclass
class _Evidence:
    """Everything one row's agent actually retrieved, for the grounding pass.

    Separate from the shared fetch cache (which dedups across rows): this is the
    per-row ground truth of what was pulled — the fetched *page texts* and the
    search *snippets* — so grounding checks the draft against real retrieved
    content, not against whatever URLs the draft cited.
    """

    pages: list[str] = field(default_factory=list)
    snippets: list[str] = field(default_factory=list)


@dataclass
class ResearchCompanySummary:
    """The run's tally, rendered by the CLI."""

    dry_run: bool = False
    redo: bool = False
    considered: int = 0          # rows selected for research
    briefed: int = 0             # rows we stored a usable (grounded) brief for
    needs_review: int = 0        # brief empty after grounding / agent gave nothing
    from_wttj: int = 0           # of considered, those with a deterministic WTTJ profile
    skipped_no_company: int = 0  # anonymous offers with no company name to research


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
                "skipped_no_company": summary.skipped_no_company,
            })
        conn.close()

    return summary


def _build_tools(searxng, search_client, fetch_client, cache, evidence) -> dict:
    """Bind the whitelisted tools for one row, capturing everything retrieved.

    Both wrappers record what they actually pulled into ``evidence`` (a per-row
    ``_Evidence``), because the grounding pass must check the draft against what
    we *retrieved*, not against the URLs the draft happens to cite. The Doctolib
    smoke run showed why: the model cited three URLs but only one was fetched, and
    citing-URL matching then left grounding with a 30-char page title. So we
    capture:
      * every search result's **snippet**, and
      * every **fetched page's extracted text** (ground truth — a page the model
        cited but never fetched is not in here; a page it fetched but didn't cite
        still is).
    The fetch cache is shared per run (dedup across rows); evidence is per row.
    """
    def web_search(query: str) -> list[dict]:
        results = tools.web_search(query, searxng_url=searxng, client=search_client)
        for r in results:
            snippet = (r.get("snippet") or "").strip()
            if snippet:
                evidence.snippets.append(snippet)
        return results

    def fetch_page(url: str) -> str:
        text = tools.fetch_page(
            url, policy=tools.POLICY_SOFT, client=fetch_client, cache=cache
        )
        # Keep only real page text — the tool returns "(fetch failed/refused…)" /
        # "(skipped…)" sentinels for errors, which aren't evidence.
        if text and not text.startswith("("):
            evidence.pages.append(text)
        return text

    return {"web_search": web_search, "fetch_page": fetch_page}


def _research_row(
    conn, row, *, searxng, search_client, fetch_client, cache, model, generate, dry_run, summary,
) -> None:
    """Research one company, fail-soft. Draft → ground → persist."""
    try:
        record = JobRecord.from_row(row)

        # No company name (anonymous France Travail postings the adapter's
        # recovery couldn't resolve) → nothing to research on. Skip the agent
        # entirely rather than searching on an empty string, but stamp the row so
        # it isn't retried every run (--redo re-attempts). The offer is still
        # filtered/enriched/scored on its own description.
        if not (record.company or "").strip():
            summary.skipped_no_company += 1
            logger.info("[%s] %r — no company name, skipping research",
                        row["source"], row["title"])
            if not dry_run:
                db.record_company_brief(conn, row["id"], company_brief_json=None)
            return

        evidence = _Evidence()
        tools_map = _build_tools(searxng, search_client, fetch_client, cache, evidence)

        # Step 1 — deterministic WTTJ company profile via the organizations
        # Algolia index (structured JSON; no LLM, no WAF-walled HTML fetch). The
        # shared HTTP client is reused for the Algolia POST.
        wttj_src, wttj_text = company_agent.fetch_wttj_profile(record, client=fetch_client)
        if wttj_text:
            summary.from_wttj += 1

        # Step 2-3 — agent fills gaps and drafts the brief.
        draft = company_agent.research_company(
            record, tools_map=tools_map, wttj_text=wttj_text, model=model, generate=generate,
        )

        # Step 4 — ground the draft against ALL evidence we have: the job
        # description (we almost always have it), the WTTJ profile (when the fetch
        # succeeded), the pages the agent actually fetched, and the search
        # snippets. A missing WTTJ profile never sinks the brief on its own — only
        # a total absence of evidence does.
        source_texts = _collect_sources(record, wttj_text, evidence)
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


def _collect_sources(record, wttj_text, evidence: "_Evidence") -> list[str]:
    """Assemble the full evidence set the grounding pass checks the draft against.

    Four kinds of evidence, most-trusted first — the point (owner's decision) is
    that the brief grounds against *whatever we retrieved*, so a failed WTTJ fetch
    never on its own leaves the fact-checker with nothing:

      1. **The job description** — untrusted posting text (fenced by the grounding
         prompt like every other source), but we almost always have it. This is
         the evidence that guarantees a missing WTTJ profile doesn't sink the brief.
      2. **The WTTJ profile** — when the deterministic fetch succeeded.
      3. **The pages the agent actually fetched** — captured in ``evidence.pages``
         as they were retrieved, NOT by re-matching the draft's cited URLs (the
         Doctolib run showed the model cites URLs it never fetched, which left
         grounding with only a page title).
      4. **Search snippets** — the fallback when a run searched but fetched nothing.

    ``verify_brief`` returns ``needs_review`` only when this whole set is empty.
    De-dups while preserving order, and keeps each page bounded (the grounding
    prompt fences + truncates each source too).
    """
    texts: list[str] = []
    seen: set[str] = set()

    def add(text: str | None) -> None:
        t = (text or "").strip()
        if t and t not in seen:
            seen.add(t)
            texts.append(t)

    add(record.description)
    add(wttj_text)
    for page in evidence.pages:
        add(page)
    for snippet in evidence.snippets:
        add(snippet)
    return texts

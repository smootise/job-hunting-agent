"""Welcome to the Jungle adapter — queries the public Algolia search index.

WTTJ's job search is backed by Algolia, and the browser talks to it with a
*public, search-only* API key. We use the same public endpoint the site's own
frontend uses. This is not a scraped secret: the key is search-only and
rate-limited by Algolia, and it's shipped to every visitor's browser. Two
non-obvious requirements we learned by probing the live endpoint:

  1. Auth goes in the query string (`x-algolia-application-id` /
     `x-algolia-api-key`), the way the JS client sends it — NOT as headers.
  2. Algolia enforces a **Referer allowlist** on this key. Without a
     `Referer: https://www.welcometothejungle.com/` header the API returns
     `403 "Method not allowed with this referer"`. So we send it.

If this ever starts 403-ing, the key has rotated: open the live site, watch
the `/queries` XHR in devtools, and copy the new `x-algolia-api-key`.

Filtering philosophy (see the Phase 1 plan): we filter *only* on keywords and
region here. Region is an Algolia facet filter on `offices.state`. Contract
type, salary, and seniority are deliberately NOT filtered at the API — those
are Phase 2's transparent, logged hard filters. So this adapter over-fetches
on purpose (internships, non-CDI, etc. come through) and lets nothing be
silently dropped upstream of our own logic.
"""

from __future__ import annotations

import json
import urllib.parse

import httpx

from jobscout import normalize
from jobscout.models import JobRecord

_ALGOLIA_URL = "https://csekhvms53-dsn.algolia.net/1/indexes/*/queries"
_JOBS_INDEX = "wk_cms_jobs_production"

# Public, search-only credentials shipped in the WTTJ frontend. Safe to inline
# (not a secret); see module docstring on rotation.
_APP_ID = "CSEKHVMS53"
_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"

# Required so Algolia's referer allowlist accepts the request.
_REFERER = "https://www.welcometothejungle.com/"

# Broad PM/PO net. Algolia treats the query as full-text, so a two-word query
# already matches "senior product manager", "AI product owner", etc. We run
# both because a single "product manager OR owner" query isn't how Algolia
# parses OR — separate queries + our dedupe is simpler and more predictable.
_QUERIES = ("product manager", "product owner")

# Region facet: WTTJ tags each office with a `state`. Île-de-France offices
# carry exactly this value (confirmed live).
_REGION_FACET = "offices.state:Ile-de-France"

_HITS_PER_PAGE = 100  # Algolia's max page size; fewer round-trips.


def fetch(
    *,
    limit: int = 200,
    client: httpx.Client | None = None,
) -> list[JobRecord]:
    """Fetch up to `limit` PM/PO offers in Île-de-France from WTTJ.

    Runs each broad query, paginates politely up to the cap, and maps every
    hit to a JobRecord. Returns possibly-duplicated records (the same job can
    match both queries, or appear twice in Algolia) — intra-batch dedupe on
    (source, external_id) is the pipeline's job, not the adapter's.

    `client` is injectable so tests can pass a stub; in production we open a
    short-lived client with a timeout (one of the project's "hard caps").
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        records: list[JobRecord] = []
        for query in _QUERIES:
            records.extend(_fetch_query(client, query, limit))
            if len(records) >= limit:
                break
        return records[:limit]
    finally:
        if owns_client:
            client.close()


def _fetch_query(
    client: httpx.Client, query: str, limit: int
) -> list[JobRecord]:
    """Paginate one query until `limit` hits or pages run out."""
    out: list[JobRecord] = []
    page = 0
    while len(out) < limit:
        body = _search_body(query, page)
        response = client.post(
            _ALGOLIA_URL,
            params={
                "x-algolia-application-id": _APP_ID,
                "x-algolia-api-key": _API_KEY,
                "x-algolia-agent": "jobscout",
            },
            headers={"Content-Type": "application/json", "Referer": _REFERER},
            json=body,
        )
        response.raise_for_status()
        result = response.json()["results"][0]
        hits = result.get("hits", [])
        out.extend(parse_hit(h) for h in hits)
        if page >= result.get("nbPages", 1) - 1 or not hits:
            break
        page += 1
    return out


def _search_body(query: str, page: int) -> dict:
    """Build the Algolia multi-query request body for one page."""
    facet_filters = json.dumps([[_REGION_FACET]])
    params = (
        f"query={urllib.parse.quote(query)}"
        f"&hitsPerPage={_HITS_PER_PAGE}"
        f"&page={page}"
        f"&facetFilters={urllib.parse.quote(facet_filters)}"
    )
    return {"requests": [{"indexName": _JOBS_INDEX, "params": params}]}


def parse_hit(hit: dict) -> JobRecord:
    """Map one Algolia hit to a JobRecord.

    Pure and side-effect-free so tests can feed it a saved fixture hit. The
    non-obvious mappings:
      - contract: `contract_type` is the *schedule* ('FULL_TIME'); the real
        contract label lives in `contract_type_names.fr` ('CDI'). We map from
        the French name, which is what the salary/contract hard filter wants.
      - url: rebuilt from org slug + job slug in the site's canonical form.
      - salary_text: assembled from the structured min/max/currency fields
        when present; None otherwise (most WTTJ offers omit salary).
    """
    org = hit.get("organization") or {}
    company = org.get("name") or ""
    slug = hit.get("slug") or hit.get("objectID") or ""
    lang = hit.get("language") or None

    contract_names = hit.get("contract_type_names") or {}
    contract_raw = contract_names.get("fr") or hit.get("contract_type")

    office = hit.get("office") or {}
    location = _format_location(office, hit.get("remote"))

    return JobRecord(
        source="wttj",
        external_id=slug,
        url=_build_url(org.get("slug"), slug, lang),
        title=hit.get("name") or "",
        company=company,
        location=location,
        contract_type=normalize.parse_contract_type(contract_raw),
        salary_text=_format_salary(hit),
        description=None,  # Algolia search hits carry no full description.
        posted_at=hit.get("published_at"),
        lang=normalize.detect_language(hit.get("name"), None) if not lang else lang,
    )


def _build_url(org_slug: str | None, job_slug: str | None, lang: str | None) -> str:
    """Reconstruct the public posting URL from slugs."""
    lang_seg = lang if lang in ("fr", "en") else "fr"
    if org_slug and job_slug:
        return (
            f"https://www.welcometothejungle.com/{lang_seg}/companies/"
            f"{org_slug}/jobs/{job_slug}"
        )
    return f"https://www.welcometothejungle.com/{lang_seg}/jobs/{job_slug or ''}"


def _format_location(office: dict, remote: str | None) -> str | None:
    """Human-readable location string, tagging remote policy when present."""
    parts = [p for p in (office.get("city"), office.get("state")) if p]
    base = ", ".join(parts) if parts else None
    if remote and remote != "no":
        return f"{base} (remote: {remote})" if base else f"remote: {remote}"
    return base


def _format_salary(hit: dict) -> str | None:
    """Assemble a salary string from structured fields, or None."""
    lo, hi = hit.get("salary_minimum"), hit.get("salary_maximum")
    if not lo and not hi:
        return None
    currency = hit.get("salary_currency") or ""
    period = hit.get("salary_period") or ""
    if lo and hi:
        span = f"{lo}-{hi}"
    else:
        span = str(lo or hi)
    return f"{span} {currency} {period}".strip()

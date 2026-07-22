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
from dataclasses import dataclass

import httpx

from jobscout import normalize
from jobscout.models import JobRecord

_ALGOLIA_URL = "https://csekhvms53-dsn.algolia.net/1/indexes/*/queries"
_JOBS_INDEX = "wk_cms_jobs_production"
# The companion company/organization index on the SAME public Algolia app. It
# returns structured company data (headcount, size band, sectors, HQ office,
# tools) as JSON — the clean alternative to fetching the WTTJ company *page*,
# which sits behind an AWS WAF JS challenge (unsolvable without a headless
# browser). Same search key, same Referer requirement as the jobs index.
_ORGS_INDEX = "wk_cms_organizations_production"

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


# ----------------------------------------------------------------------------
# Company/organization lookup (the WTTJ company-profile source)
# ----------------------------------------------------------------------------
#
# WTTJ's company *page* is behind an AWS WAF JS challenge, so we take the
# structured data from the organizations Algolia index instead (same public
# key). This is the deterministic "step 1" seed for the Phase 3 company brief:
# facts, not prose, so no grounding is needed for these fields.


@dataclass(frozen=True)
class CompanyProfile:
    """Structured company facts from WTTJ's organizations index.

    Every field is optional — WTTJ populates them unevenly. ``slug`` lets the
    caller confirm it matched the right company (query is full-text, so a name
    like "Alan" could in principle return a near-namesake)."""

    name: str
    slug: str | None
    nb_employees: int | None
    size_label: str | None       # e.g. "Between 50 and 250 employees"
    sectors: list[str]           # e.g. ["Software", "SaaS / Cloud Services"]
    tools: list[str]             # tech stack, e.g. ["Python", "React JS"]
    hq_city: str | None
    hq_state: str | None         # region, e.g. "Ile-de-France"
    website: str | None
    labels: list[str]            # e.g. ["bcorp"]


def fetch_organization(
    name: str,
    *,
    slug: str | None = None,
    client: httpx.Client | None = None,
) -> CompanyProfile | None:
    """Look up a company in WTTJ's organizations index; return its profile or None.

    Queries by ``name`` (Algolia full-text). When ``slug`` is given (recovered
    from the offer URL), we require the top hit's slug to match it — a cheap guard
    against returning a wrong near-namesake company. Fail-soft: any transport/
    parse error or no hit yields ``None`` (the caller then works without the WTTJ
    seed). ``client`` is injectable for offline tests.
    """
    if not name or not name.strip():
        return None

    owns_client = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        body = {"requests": [{
            "indexName": _ORGS_INDEX,
            "params": f"query={urllib.parse.quote(name.strip())}&hitsPerPage=1",
        }]}
        resp = client.post(
            _ALGOLIA_URL,
            params={"x-algolia-application-id": _APP_ID, "x-algolia-api-key": _API_KEY,
                    "x-algolia-agent": "jobscout"},
            headers={"Content-Type": "application/json", "Referer": _REFERER},
            json=body,
        )
        resp.raise_for_status()
        hits = resp.json()["results"][0].get("hits") or []
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    finally:
        if owns_client:
            client.close()

    if not hits:
        return None
    hit = hits[0]
    if slug and hit.get("slug") and hit["slug"] != slug:
        return None  # matched a different company — don't trust it.
    return _parse_organization(hit)


def _parse_organization(hit: dict) -> CompanyProfile:
    """Map an organizations-index hit to a ``CompanyProfile`` (English labels)."""
    size = hit.get("size") or {}
    hq = next(
        (o for o in (hit.get("offices") or []) if o.get("is_headquarter")),
        None,
    ) or (hit.get("offices") or [None])[0] or {}
    return CompanyProfile(
        name=hit.get("name") or "",
        slug=hit.get("slug"),
        nb_employees=hit.get("nb_employees") if isinstance(hit.get("nb_employees"), int) else None,
        size_label=size.get("en") if isinstance(size, dict) else None,
        sectors=_flatten_named(hit.get("sectors_name")),
        tools=_flatten_named(hit.get("tools_name")),
        hq_city=hq.get("city"),
        hq_state=hq.get("state"),
        website=(hit.get("website") or {}).get("reference") if isinstance(hit.get("website"), dict) else None,
        labels=[str(x) for x in (hit.get("labels") or []) if x],
    )


def _flatten_named(value) -> list[str]:
    """Flatten WTTJ's ``{lang: [{category: label}, …]}`` shape to English labels.

    ``sectors_name``/``tools_name`` are localized dicts of category→label pairs.
    We take the English list and keep the leaf labels ("Software", "Python"),
    de-duplicated in order. Tolerant of the plain-list form too."""
    if isinstance(value, dict):
        items = value.get("en") or next(iter(value.values()), [])
    elif isinstance(value, list):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        label = list(item.values())[0] if isinstance(item, dict) and item else (item if isinstance(item, str) else None)
        if label and label not in out:
            out.append(str(label))
    return out

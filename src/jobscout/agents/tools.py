"""The whitelisted tools the research agents may call — ordinary functions.

Two tools, both read-only, both fail-soft:

  * ``web_search`` — query the owner's self-hosted **SearXNG** (LAN, JSON API).
    Aggregates many engines; nothing leaves the machine except the query itself
    (to the LAN instance, which then queries engines). Returns a small list of
    ``{title, url, snippet}``.
  * ``fetch_page`` — a read-only HTTP GET that extracts readable text from a page,
    truncated to a hard cap. Governed by a domain **policy**: ``soft_allowlist``
    (prefer high-signal domains, allow others but log loudly) for the research
    agents, or ``hard_whitelist`` (refuse anything off-list) for the future
    cover-letter agent.

The agent loop (``loop.py``) is the only caller; it fences every result as
untrusted data. These functions add the *capability* limits CLAUDE.md requires:
no writes, no shell, hard response-size and result-count caps, request timeouts,
and a per-run fetch cache so the two agents never fetch the same page twice.

Security notes:
  * ``fetch_page`` never follows a URL scheme other than http/https, and applies
    the domain policy *before* the request — an off-whitelist host in
    ``hard_whitelist`` mode is refused without a network call.
  * All errors are swallowed into an empty/short result (fail-soft) so a dead
    link or a blocked search can never crash the batch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("jobscout.agents.tools")

# Hard caps (CLAUDE.md: "hard caps everywhere").
MAX_SEARCH_RESULTS = 6
MAX_PAGE_CHARS = 6000          # truncate extracted page text to keep prompts bounded.
FETCH_TIMEOUT = 15.0
SEARCH_TIMEOUT = 15.0
MAX_FETCH_BYTES = 2_000_000    # don't download more than ~2 MB of a page body.

# High-signal domains for the research agents' soft allowlist. Off-list domains
# are still fetched (soft policy) but logged loudly so a human reviewing the logs
# sees where the agent wandered. Matched as a suffix so subdomains count
# (e.g. ``www.societe.com``, ``fr.linkedin.com``).
SOFT_ALLOWLIST: tuple[str, ...] = (
    "welcometothejungle.com",
    "societe.com",
    "pagesjaunes.fr",
    "linkedin.com",
    "verif.com",
    "infogreffe.fr",
)

POLICY_SOFT = "soft_allowlist"
POLICY_HARD = "hard_whitelist"

# A plain browser User-Agent. Many public sites (Welcome to the Jungle among them)
# return 403 to a non-browser UA, which would silently kill the deterministic
# company-profile fetch. This is a read-only GET of a public page — within the
# brief's "public, low-volume" spirit — and reuses the same UA the LinkedIn guest
# enrichment already sends (see pipeline/enrich_linkedin.py).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "fr,en;q=0.8",
}


@dataclass
class FetchCache:
    """A per-run cache of fetched page text, shared across both agents.

    Keyed by URL. The two agents (address + company) often want the same company
    site, and within one agent the model may re-request a page; caching makes
    ``fetch_page`` idempotent within a run and avoids hammering a host. Not
    persisted — it lives for one pipeline run only.
    """

    _pages: dict[str, str] = field(default_factory=dict)

    def get(self, url: str) -> str | None:
        return self._pages.get(url)

    def put(self, url: str, text: str) -> None:
        self._pages[url] = text


@dataclass(frozen=True)
class SearchResult:
    """One search hit. Kept tiny — title + url + a short snippet is all the model
    needs to decide which page to fetch."""

    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


# --------------------------------------------------------------------------
# web_search — self-hosted SearXNG JSON API
# --------------------------------------------------------------------------


def web_search(
    query: str,
    *,
    searxng_url: str,
    client: httpx.Client | None = None,
    max_results: int = MAX_SEARCH_RESULTS,
) -> list[dict[str, str]]:
    """Search via SearXNG's JSON API; return up to ``max_results`` hits.

    Hits ``{searxng_url}/search?q=…&format=json`` (the ``format=json`` output the
    owner enabled in SearXNG settings — see ``docs/agents.md``). Fail-soft:
    returns ``[]`` on any transport/parse error or an empty query, so a blocked
    or down search never crashes the agent — the model simply sees no results and
    proceeds with what it has. ``client`` is injectable for offline tests.
    """
    if not query or not query.strip():
        return []

    owns_client = client is None
    client = client or httpx.Client(timeout=SEARCH_TIMEOUT)
    try:
        resp = client.get(
            f"{searxng_url.rstrip('/')}/search",
            params={"q": query.strip(), "format": "json"},
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("web_search failed for %r: %s", query, type(exc).__name__)
        return []
    finally:
        if owns_client:
            client.close()

    results = body.get("results") or []
    out: list[dict[str, str]] = []
    for r in results[:max_results]:
        url = r.get("url") or ""
        if not url:
            continue
        out.append(
            SearchResult(
                title=(r.get("title") or "").strip(),
                url=url,
                snippet=(r.get("content") or "").strip(),
            ).as_dict()
        )
    return out


# --------------------------------------------------------------------------
# fetch_page — read-only GET + readable-text extraction, domain-policed
# --------------------------------------------------------------------------


def _host_allowed(url: str, policy: str) -> tuple[bool, str | None]:
    """Decide whether ``url`` may be fetched under ``policy``.

    Returns ``(allowed, warning)``. In ``hard_whitelist`` an off-list host is
    refused (allowed=False). In ``soft_allowlist`` everything http/https is
    allowed, but an off-list host comes back with a warning string the caller
    logs loudly. A non-http(s) scheme is always refused (no file://, no data:).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"refused non-http(s) URL: {url!r}"
    host = (parsed.hostname or "").lower()
    on_list = any(host == d or host.endswith("." + d) for d in SOFT_ALLOWLIST)
    if policy == POLICY_HARD:
        if on_list:
            return True, None
        return False, f"refused off-whitelist host {host!r} (hard_whitelist)"
    # soft: allow, but flag off-list hosts for the human reading the logs.
    if on_list:
        return True, None
    return True, f"off-allowlist fetch: {host!r} (allowed under soft policy)"


def fetch_page(
    url: str,
    *,
    policy: str = POLICY_SOFT,
    client: httpx.Client | None = None,
    cache: FetchCache | None = None,
    timeout: float = FETCH_TIMEOUT,
    max_chars: int = MAX_PAGE_CHARS,
) -> str:
    """Read-only GET ``url`` and return its readable text (truncated).

    Governed by ``policy`` (``soft_allowlist`` default for research agents,
    ``hard_whitelist`` for the letter agent). Extracts visible text via
    BeautifulSoup (same idiom as ``adapters/linkedin_guest``), stripping script/
    style, and truncates to ``max_chars`` so a huge page can't blow the prompt.
    Fail-soft: a refused host, a timeout, an HTTP error, or a non-HTML body all
    return a short human-readable ``"(...)"`` string the model can read — never a
    raise. ``cache`` (per-run) makes repeat fetches free and polite.
    """
    if cache is not None:
        cached = cache.get(url)
        if cached is not None:
            return cached

    allowed, warning = _host_allowed(url, policy)
    if warning and allowed:
        logger.warning("fetch_page %s", warning)  # loud line for off-list soft fetches.
    if not allowed:
        logger.warning("fetch_page %s", warning)
        return f"(fetch refused: {warning})"

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        resp = client.get(url, headers=_BROWSER_HEADERS)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return f"(skipped non-text page: {content_type or 'unknown type'})"
        html = resp.text[:MAX_FETCH_BYTES]
    except httpx.HTTPError as exc:
        logger.warning("fetch_page failed for %s: %s", url, type(exc).__name__)
        return f"(fetch failed: {type(exc).__name__})"
    finally:
        if owns_client:
            client.close()

    text = _extract_text(html)
    truncated = text[:max_chars]
    if len(text) > max_chars:
        truncated += "\n…(truncated)"
    if cache is not None:
        cache.put(url, truncated)
    return truncated


def _extract_text(html: str) -> str:
    """Visible text from an HTML document, scripts/styles/nav stripped.

    Deliberately simple (a readability library is overkill for our need — the
    model just wants the words). Drops non-content tags, then collapses to a
    newline-joined block. Same BeautifulSoup + ``get_text`` approach as the
    LinkedIn guest adapter.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # Collapse runs of blank lines the strip leaves behind.
    lines = [ln for ln in (line.strip() for line in text.splitlines()) if ln]
    return "\n".join(lines)

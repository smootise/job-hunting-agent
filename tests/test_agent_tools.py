"""Tests for the whitelisted agent tools (web_search + fetch_page).

Fully offline: httpx.MockTransport clients stand in for SearXNG and the open web.
Covers web_search JSON mapping + fail-soft + caps, and fetch_page text
extraction, the soft/hard domain policy, the per-run cache, and the scheme /
content-type guards.
"""

from __future__ import annotations

import httpx

from jobscout.agents import tools


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- web_search ----------------------------------------------------------


def test_web_search_maps_results():
    def handler(request):
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            {"title": "ACME", "url": "https://acme.fr", "content": "AI tools"},
            {"title": "NoURL", "url": "", "content": "drop me"},
        ]})

    res = tools.web_search("acme", searxng_url="http://nas:8085", client=_client(handler))
    assert len(res) == 1  # empty-URL hit dropped
    assert res[0] == {"title": "ACME", "url": "https://acme.fr", "snippet": "AI tools"}


def test_web_search_empty_query_returns_empty():
    assert tools.web_search("   ", searxng_url="http://x", client=_client(lambda r: httpx.Response(200, json={}))) == []


def test_web_search_failsoft_on_error():
    def boom(request):
        raise httpx.ConnectError("down")

    assert tools.web_search("q", searxng_url="http://x", client=_client(boom)) == []


def test_web_search_caps_results():
    many = {"results": [{"title": str(i), "url": f"http://x/{i}", "content": ""} for i in range(50)]}
    res = tools.web_search("q", searxng_url="http://x", client=_client(lambda r: httpx.Response(200, json=many)),
                           max_results=3)
    assert len(res) == 3


# --- fetch_page ----------------------------------------------------------

_HTML = ("<html><head><style>x{}</style></head><body>"
         "<script>evil()</script><h1>ACME</h1>"
         "<p>12 rue de Rivoli, 75001 Paris. We build AI tools.</p></body></html>")


def _html_client():
    return _client(lambda r: httpx.Response(200, text=_HTML, headers={"content-type": "text/html"}))


def test_fetch_page_extracts_text_drops_scripts():
    txt = tools.fetch_page("https://acme.fr/about", client=_html_client())
    assert "ACME" in txt and "rue de Rivoli" in txt
    assert "evil()" not in txt  # script stripped


def test_fetch_page_sends_browser_user_agent():
    # Many public sites (Welcome to the Jungle) 403 a non-browser UA, which would
    # silently kill the deterministic company-profile fetch. Assert we send a
    # browser UA so that stays working.
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text=_HTML, headers={"content-type": "text/html"})

    tools.fetch_page("https://acme.fr/about", client=_client(handler))
    assert "Mozilla/5.0" in seen["ua"]
    assert "jobscout" not in seen["ua"].lower()  # not the old non-browser UA


def test_fetch_page_cache_avoids_second_request():
    cache = tools.FetchCache()
    tools.fetch_page("https://acme.fr/about", client=_html_client(), cache=cache)

    def must_not_call(request):
        raise AssertionError("cache should have served this")

    txt = tools.fetch_page("https://acme.fr/about", client=_client(must_not_call), cache=cache)
    assert "ACME" in txt


def test_fetch_page_hard_whitelist_refuses_offlist():
    def must_not_call(request):
        raise AssertionError("must not fetch off-list under hard policy")

    r = tools.fetch_page("https://evil.example/x", policy=tools.POLICY_HARD, client=_client(must_not_call))
    assert "refused" in r


def test_fetch_page_hard_whitelist_allows_onlist():
    r = tools.fetch_page("https://www.societe.com/acme", policy=tools.POLICY_HARD, client=_html_client())
    assert "ACME" in r


def test_fetch_page_soft_allows_offlist(caplog):
    # soft policy fetches an off-list host, and logs loudly.
    import logging
    with caplog.at_level(logging.WARNING, logger="jobscout.agents.tools"):
        r = tools.fetch_page("https://random.example/x", policy=tools.POLICY_SOFT, client=_html_client())
    assert "ACME" in r
    assert any("off-allowlist" in rec.message for rec in caplog.records)


def test_fetch_page_refuses_non_http_scheme():
    assert "refused" in tools.fetch_page("file:///etc/passwd")


def test_fetch_page_skips_non_text():
    def pdf(request):
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

    assert "skipped non-text" in tools.fetch_page("https://acme.fr/x.pdf", client=_client(pdf))


def test_fetch_page_failsoft_on_http_error():
    def err(request):
        return httpx.Response(500, text="boom")

    assert "fetch failed" in tools.fetch_page("https://acme.fr/x", client=_client(err))


def test_fetch_page_truncates_long_pages():
    big = "<html><body>" + ("word " * 5000) + "</body></html>"
    r = tools.fetch_page("https://acme.fr/big",
                         client=_client(lambda rq: httpx.Response(200, text=big, headers={"content-type": "text/html"})),
                         max_chars=100)
    assert "truncated" in r
    assert len(r) < 200

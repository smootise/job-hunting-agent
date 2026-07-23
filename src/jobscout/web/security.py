"""A minimal same-origin guard for the webapp's write/run routes.

Threat model. The app binds ``127.0.0.1`` only (``Settings.host`` default;
``serve`` never binds ``0.0.0.0``), is single-user, and has no auth, cookies, or
sessions. The classic CSRF attack — a malicious site auto-submitting a form to a
victim's *authenticated* session — needs ambient credentials this app doesn't
have, so a token scheme would be ceremony without benefit. The residual risk is
a malicious web page in the same browser POSTing to ``127.0.0.1:<port>``. A
same-origin check on ``Origin``/``Referer`` closes that at ~no cost.

So: reject a state-changing request whose ``Origin`` (or, failing that,
``Referer``) is *present and* not one of this app's own loopback origins. A
*missing* Origin/Referer is allowed — same-origin HTMX requests and
non-browser clients (curl, the test client) legitimately omit it, and without
credentials there's nothing to steal anyway. This is deliberately not a full
CSRF-token implementation; see the module docstring rationale.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException, Request


def _allowed_origins(port: int) -> set[str]:
    """The loopback origins this app serves itself on, for a given port."""
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }


def require_local_origin(request: Request) -> None:
    """Reject a write request coming from a foreign origin (FastAPI dependency).

    Allows the request when neither ``Origin`` nor ``Referer`` is present (the
    common same-origin HTMX / non-browser case). When one *is* present, it must
    match a loopback origin on this app's port, else 403. Read routes don't use
    this — only writes (review + run triggers).
    """
    port = request.app.state.settings.port
    allowed = _allowed_origins(port)

    origin = request.headers.get("origin")
    if origin is not None:
        if origin not in allowed:
            raise HTTPException(status_code=403, detail="cross-origin request refused")
        return

    # No Origin (some browsers omit it on same-origin form posts) — fall back to
    # the Referer's scheme+host+port if present.
    referer = request.headers.get("referer")
    if referer is not None:
        parsed = urlparse(referer)
        referer_origin = f"{parsed.scheme}://{parsed.netloc}"
        if referer_origin not in allowed:
            raise HTTPException(status_code=403, detail="cross-origin request refused")

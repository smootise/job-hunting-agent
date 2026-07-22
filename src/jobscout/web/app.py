"""The FastAPI application factory.

``create_app`` wires settings → templates → routes and caches the scoring rubric
(criteria names/weights, from the gitignored ``preferences.yaml``) on
``app.state`` so every request reuses it without re-reading the file. A
module-level ``app = create_app()`` exists for the ``uvicorn jobscout.web.app:app``
import path and ``--reload``; ``jobscout serve`` calls the factory the same way.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from jobscout import config

from .routes import router
from .settings import Settings, resolve_settings
from .templating import build_templates

_STATIC_DIR = Path(__file__).parent / "static"


def _load_criteria(preferences_path: Path) -> list[dict]:
    """Read the rubric criteria (name/weight/description) from preferences.

    Returns an empty list if preferences can't be loaded (e.g. the gitignored
    real file is absent on a fresh checkout) rather than crashing the app — the
    detail page's score breakdown just renders nothing, and the rest works. The
    ``serve`` command's job is browsing existing data, not enforcing config.
    """
    try:
        prefs = config.load_preferences(preferences_path)
    except Exception:  # noqa: BLE001 — best-effort load; missing/bad prefs → no breakdown
        return []
    rubric = (prefs or {}).get("scoring_rubric", {}) or {}
    return list(rubric.get("criteria", []) or [])


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Pass ``settings`` (e.g. a fixture DB) or resolve defaults."""
    settings = settings or resolve_settings()

    app = FastAPI(title="Job Scout", docs_url=None, redoc_url=None)

    criteria = _load_criteria(settings.preferences_path)
    app.state.settings = settings
    app.state.templates = build_templates()
    app.state.criteria = criteria
    app.state.criteria_names = frozenset(c["name"] for c in criteria if c.get("name"))

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(router)
    return app


app = create_app()

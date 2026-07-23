"""Resolved paths + bind address for the webapp.

The CLI uses CWD-relative defaults (``Path("data/jobs.db")``,
``Path("preferences.yaml")``) — fine for ``jobscout <cmd>`` run from the repo
root, but a web server can be launched from anywhere (a service manager, an IDE,
``--reload`` re-execs). So the webapp resolves an explicit **project root** by
walking up from this file to the ``pyproject.toml`` marker, then anchors the DB
and preferences paths to it. Env overrides (``JOBSCOUT_DB_PATH`` /
``JOBSCOUT_PREFERENCES_PATH``) let tests and alternate deployments point
elsewhere without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    """Walk upward from ``start`` until a directory holds ``pyproject.toml``.

    Falls back to ``start`` if no marker is found (e.g. an unusual install
    layout) rather than raising — the env overrides still let a caller pin the
    real paths, and a wrong root only means "DB not found", surfaced clearly.
    """
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return start


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to find its data and bind its socket.

    ``env_path`` is the project-root-anchored ``.env`` — the background runner
    passes it to the stages that need credentials (enrich-commute, research), so
    a server launched from another directory still finds the real ``.env`` rather
    than the ``run_*`` CWD-relative default.
    """

    project_root: Path
    db_path: Path
    preferences_path: Path
    env_path: Path
    host: str = "127.0.0.1"
    port: int = 8020


def resolve_settings(
    *,
    db_path: Path | None = None,
    preferences_path: Path | None = None,
    env_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8020,
) -> Settings:
    """Build ``Settings``, honoring explicit args, then env vars, then defaults.

    Precedence for each path: explicit argument (tests inject a fixture DB) →
    ``JOBSCOUT_DB_PATH`` / ``JOBSCOUT_PREFERENCES_PATH`` env var → the
    project-root-anchored default. ``host``/``port`` follow the same pattern via
    ``JOBSCOUT_HOST``/``JOBSCOUT_PORT`` — this matters because ``serve`` runs the
    *module-level* ``app`` (built by ``create_app()`` with no args), so the CLI
    hands the chosen port through the environment rather than as an argument. The
    port must reach ``Settings`` or the same-origin guard would reject requests to
    a non-default port. ``host`` defaults to loopback; ``serve`` never binds
    ``0.0.0.0`` — this is a single-user local tool.
    """
    root = _find_project_root(Path(__file__).resolve())

    resolved_db = (
        db_path
        or _env_path("JOBSCOUT_DB_PATH")
        or root / "data" / "jobs.db"
    )
    resolved_prefs = (
        preferences_path
        or _env_path("JOBSCOUT_PREFERENCES_PATH")
        or root / "preferences.yaml"
    )
    resolved_env = env_path or _env_path("JOBSCOUT_ENV_PATH") or root / ".env"
    resolved_host = os.environ.get("JOBSCOUT_HOST", host)
    port_env = os.environ.get("JOBSCOUT_PORT")
    resolved_port = int(port_env) if port_env else port
    return Settings(
        project_root=root,
        db_path=resolved_db,
        preferences_path=resolved_prefs,
        env_path=resolved_env,
        host=resolved_host,
        port=resolved_port,
    )


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None

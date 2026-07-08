"""Loads and validates preferences.yaml.

preferences.yaml is gitignored (it contains the owner's home location) and
is the single source of truth for hard filters and the scoring rubric. This
module's job is just to load it into a typed shape — the *interpretation*
of each field (e.g. the commute-filter null-means-no-filter trap, the
whole-token seniority matching) lives in the pipeline stage that consumes
it, not here. Keeping that logic close to its usage is what CLAUDE.md's
"How to interpret preferences.yaml" section is documenting against.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PREFERENCES_PATH = Path("preferences.yaml")
DEFAULT_ENV_PATH = Path(".env")


def load_preferences(path: Path = DEFAULT_PREFERENCES_PATH) -> dict[str, Any]:
    """Read preferences.yaml and return it as a plain dict.

    Deliberately returns a dict rather than a frozen dataclass for now:
    the schema is still settling (see preferences.example.yaml), and a
    thin loader keeps this file small while the pipeline stages are built.
    Revisit with typed models once the schema stabilizes.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy preferences.example.yaml to "
            f"{path} and fill in your own values (this file is gitignored)."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------
# Secrets (.env) — credentials for the sources that need them
# --------------------------------------------------------------------------
#
# We hand-roll a tiny .env reader instead of pulling in python-dotenv: the
# file has ~5 keys, the format is trivial (KEY=VALUE lines), and avoiding a
# dependency for that keeps the surface small. Values already present in the
# real process environment win over the file, so CI or a shell export can
# override without editing .env. Secrets are read here and passed explicitly
# to adapters — they are never logged and never enter an LLM prompt
# (CLAUDE.md's Security invariants).


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Parse a .env file into a dict, merged under the real environment.

    Missing file is not an error — a run using only WTTJ (no credentials)
    must work with no .env at all. Blank lines and `#` comments are skipped;
    surrounding quotes on a value are stripped. The live `os.environ` takes
    precedence over file values.
    """
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    # Real environment overrides the file.
    for key in list(values):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


@dataclass(frozen=True)
class FranceTravailCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class ImapCredentials:
    host: str
    user: str
    app_password: str


def _require(env: dict[str, str], *keys: str) -> list[str]:
    """Return the values for `keys`, or raise a clear, actionable error.

    The error names exactly which keys are missing and points at .env, so a
    half-configured source fails loudly at startup rather than deep inside an
    HTTP call with an opaque 401.
    """
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing required credential(s) {', '.join(missing)}. "
            f"Set them in .env (copy from .env.example)."
        )
    return [env[k] for k in keys]


def france_travail_credentials(
    env: dict[str, str] | None = None,
) -> FranceTravailCredentials:
    """FT client id/secret, or a clear error if not configured."""
    env = env if env is not None else load_env()
    client_id, client_secret = _require(
        env, "FRANCE_TRAVAIL_ID", "FRANCE_TRAVAIL_SECRET"
    )
    return FranceTravailCredentials(client_id, client_secret)


def imap_credentials(env: dict[str, str] | None = None) -> ImapCredentials:
    """IMAP host/user/app-password, or a clear error if not configured."""
    env = env if env is not None else load_env()
    host, user, password = _require(
        env, "IMAP_HOST", "IMAP_USER", "IMAP_APP_PASSWORD"
    )
    return ImapCredentials(host, user, password)

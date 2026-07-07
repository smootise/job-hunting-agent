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

from pathlib import Path
from typing import Any

import yaml

DEFAULT_PREFERENCES_PATH = Path("preferences.yaml")


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

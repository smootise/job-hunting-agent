"""The deterministic pipeline: normalize, dedupe, and hard-filter offers.

This is plain Python, no LLM calls. Hard filters (remote policy, contract
type, salary floor, seniority keywords, company blocklist) run here,
cheaply and deterministically, before anything reaches enrichment or
scoring — see CLAUDE.md's "How to interpret preferences.yaml" for the
non-obvious matching rules each filter must follow.
"""

from __future__ import annotations

from collections.abc import Callable

# The optional progress callback every ``run_*`` stage accepts: called after each
# processed unit (offer, or source for ingest) with ``(done, total)``. Its whole
# purpose is a UI progress bar — the webapp's background runner passes one in; the
# CLI passes nothing (default None), so the CLI path is unaffected. Kept here so
# all seven stages share one definition instead of redeclaring it.
ProgressFn = Callable[[int, int], None]


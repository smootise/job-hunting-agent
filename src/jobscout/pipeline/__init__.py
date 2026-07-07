"""The deterministic pipeline: normalize, dedupe, and hard-filter offers.

This is plain Python, no LLM calls. Hard filters (remote policy, contract
type, salary floor, seniority keywords, company blocklist) run here,
cheaply and deterministically, before anything reaches enrichment or
scoring — see CLAUDE.md's "How to interpret preferences.yaml" for the
non-obvious matching rules each filter must follow.
"""

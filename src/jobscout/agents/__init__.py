"""The two agentic components, built on shared hand-rolled tool-loop machinery.

Both agents follow the same loop: build prompt -> call Ollama -> parse
structured response -> execute one whitelisted tool -> append result to
context -> repeat until done or a step cap is hit. No agent framework —
the loop is intentionally readable so it can be studied.

1. Address-research agent (built first, the warm-up): web_search +
   fetch_page, output is one schema-validated address candidate,
   validated deterministically outside the agent. No write tools.
2. Cover-letter agent: fetch_page (per-job domain whitelist) +
   read_profile + save_draft (confined to output/letters/). Adapts the
   owner's master letter — never writes from scratch.

Both treat fetched web content as untrusted input (see CLAUDE.md's
Security invariants) and log every step to logs/ for replay and study.
"""

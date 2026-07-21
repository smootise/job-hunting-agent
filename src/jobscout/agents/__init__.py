"""Phase 3 — the hand-rolled agent tool loop and the research agents.

This package is the project's agent-mechanics lesson (CLAUDE.md: "transparency
over magic, no agent framework in v1"). Every agent follows the same loop, in
``loop.py``: build prompt -> call Ollama -> parse a JSON action -> run one
whitelisted tool -> append the observation -> repeat until a final answer or a
hard step cap. No LangGraph, no Pydantic AI — the loop is intentionally readable
so it can be studied.

Modules:
  * ``loop``    — the shared ReAct-style tool loop (the learning artifact).
  * ``tools``   — the whitelisted tools: ``web_search`` (self-hosted SearXNG) and
                  read-only ``fetch_page``. No write tools, no profile access.
  * ``address_agent``  — warm-up agent: company + city -> one office-address
                  candidate, validated deterministically *outside* the agent
                  (BAN geocode + Île-de-France check). A wrong address can never
                  hard-reject an offer.
  * ``company_agent``  — enrich an offer with a grounded company brief for the
                  scorer: WTTJ profile deterministically, then the agent fills
                  gaps from the company site + web search, then a grounding pass.

The cover-letter agent (the higher-stakes second lesson) reuses this same loop
and is built after these two.

Security posture (see each module + CLAUDE.md): all fetched/searched text is
wrapped in explicit delimiters and labeled untrusted-data-not-instructions, and
every model exchange is logged in full to ``logs/`` for replay and study.
"""

"""Thin Ollama client wrapper with full prompt/response logging.

Every call to a local model — pipeline scoring, agent tool-loop steps,
bake-off harness runs — goes through here. CLAUDE.md is explicit that
every LLM interaction must be logged in full (prompt in, response out):
these logs are the study material for how the agent mechanics actually
behave, not just an operational nicety.
"""

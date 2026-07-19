"""The hand-rolled agent tool loop — Phase 3's core learning artifact.

This is a ReAct-style loop written from scratch, no framework, so every moving
part of "an agent" is visible and studyable (CLAUDE.md: transparency over magic).
The whole idea of an agent, stripped of libraries, is this cycle:

    build a prompt  ->  ask the model what to do next
                    ->  it either calls a TOOL or gives a FINAL answer
    if a tool:      ->  run that (whitelisted) Python function
                    ->  append its result to the running transcript as an
                        OBSERVATION, and loop again
    if final:       ->  stop and hand the answer back to the caller

Three design choices worth understanding, because they're the agent lesson:

1. **Context assembly is just string concatenation.** The model is stateless; it
   only "remembers" what we put back in the prompt each turn. So the loop keeps a
   growing ``transcript`` of (action, observation) pairs and re-sends it every
   step. That list *is* the agent's working memory — there's no magic.

2. **The stop condition is explicit and bounded.** The model signals "done" by
   emitting ``{"final": ...}``. But a local model can loop, so there is a hard
   ``max_steps`` cap; when it's hit we make one last call that *forbids* tools and
   forces a final answer. An agent that can't stop itself is a bug, so we make
   stopping the loop's responsibility, not the model's goodwill.

3. **Tools are an explicit whitelist passed in.** The loop can only call the
   functions in the ``tools`` dict it's handed. There is no ``eval``, no shell,
   no dynamic import — a tool is an ordinary Python callable. This is the whole
   security model: capability = the tool list, nothing more (CLAUDE.md).

Security (CLAUDE.md): every tool observation — which for the research agents is
untrusted web text — is wrapped in explicit delimiters and labeled "data, never
instructions", so an injected "ignore your instructions" in a fetched page sits
inside a clearly-marked data block. Validation of the *final* answer is the
caller's job (each agent has its own schema); the loop stays schema-agnostic.

The loop does NOT validate the final answer's shape or retry on a bad one — that
belongs to each agent (they have different schemas). The loop's contract is
narrow: run the tool cycle, return whatever ``final`` payload the model produced
plus a full step trace, or signal that it never produced one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jobscout.llm import client as llm_client

logger = logging.getLogger("jobscout.agents.loop")

# A tool is any callable taking keyword args and returning a JSON-serializable
# result (str / dict / list). Tools raise on hard errors; the loop catches and
# feeds the error back as an observation so the model can recover or give up.
Tool = Callable[..., Any]

# The signature of the text-generation call — matches ``llm_client.generate`` but
# is injectable so tests drive the loop with a canned script (no Ollama, no net).
GenerateFn = Callable[..., "llm_client.GenerationResult"]

DEFAULT_MODEL = "qwen3.6:35b-a3b"
DEFAULT_MAX_STEPS = 6

# Delimiters that fence untrusted tool output in the transcript. An injected
# instruction inside a fetched page lands *inside* this block, which the system
# prompt tells the model to treat as data only. Same discipline as the scorer's
# ``scoring._posting_block``.
_OBS_OPEN = "<<<OBSERVATION"
_OBS_CLOSE = "OBSERVATION>>>"


@dataclass
class Step:
    """One turn of the loop, kept for the trace the caller logs/returns.

    ``raw`` is the model's exact text that turn (already logged in full by
    ``llm_client`` too); ``tool``/``args``/``observation`` are set when the turn
    was a tool call; ``final`` is set on the turn that ended the loop.
    """

    index: int
    raw: str
    tool: str | None = None
    args: dict[str, Any] | None = None
    observation: str | None = None
    final: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class AgentResult:
    """What ``run_agent`` returns: the final payload (or None) + the full trace.

    ``final`` is the model's ``{"final": <payload>}`` payload — the caller
    validates its shape. It is ``None`` when the loop hit ``max_steps`` without
    the model (even when forced) producing a parseable final answer, so the
    caller can mark the row ``needs_review`` rather than trust a non-answer.
    ``steps`` is the ordered trace (tool calls + observations) for study/logging.
    """

    final: dict[str, Any] | None
    steps: list[Step] = field(default_factory=list)
    stopped_reason: str = ""  # "final" | "max_steps" | "forced_final" | "no_final"

    @property
    def succeeded(self) -> bool:
        return self.final is not None


def run_agent(
    system: str,
    task: str,
    tools: dict[str, Tool],
    *,
    model: str = DEFAULT_MODEL,
    max_steps: int = DEFAULT_MAX_STEPS,
    generate: GenerateFn | None = None,
    log_dir: Path = llm_client.DEFAULT_LOG_DIR,
) -> AgentResult:
    """Run the tool loop until the model gives a final answer or ``max_steps``.

    ``system`` is the agent's fixed instructions (task, tool contract, output
    schema, injection warning). ``task`` is the specific job this run (e.g. the
    company + city). ``tools`` is the whitelist — the model can call nothing
    else. ``generate`` is injectable so tests script the model's turns offline.

    Returns an ``AgentResult`` whose ``final`` the *caller* validates against its
    own schema. The loop guarantees termination (the ``max_steps`` cap + a forced
    final turn) and never raises on a tool error — a failing tool becomes an
    observation the model can react to.
    """
    generate = generate or llm_client.generate
    transcript: list[str] = []
    steps: list[Step] = []

    for index in range(1, max_steps + 1):
        prompt = _build_prompt(task, transcript, tools)
        gen = generate(model, prompt, system=system, log_dir=log_dir)
        step = Step(index=index, raw=gen.response)

        action = _parse_action(gen.response)
        if action is None:
            # Not parseable as an action at all. Record it and prod the model to
            # emit valid JSON next turn (it often self-corrects). This is not a
            # tool error; it's a format error, so we don't burn a "final".
            step.error = "unparseable action (expected tool call or final JSON)"
            steps.append(step)
            transcript.append(_format_turn(gen.response, _format_error(step.error)))
            logger.info("[step %d] unparseable action", index)
            continue

        if "final" in action:
            step.final = action["final"]
            steps.append(step)
            logger.info("[step %d] final answer", index)
            return AgentResult(final=action["final"], steps=steps, stopped_reason="final")

        # Otherwise it's a tool call: {"tool": name, "args": {...}}.
        name = action.get("tool")
        args = action.get("args") or {}
        step.tool, step.args = name, args
        observation = _dispatch(name, args, tools, step)
        step.observation = observation
        steps.append(step)
        transcript.append(_format_turn(gen.response, observation))
        logger.info("[step %d] tool=%s", index, name)

    # Step budget exhausted without a final answer: make ONE more call that
    # forbids tools and demands the final JSON now. An agent that can't stop is a
    # bug — stopping is the loop's job, not the model's discretion.
    return _force_final(system, task, transcript, tools, model, generate, log_dir, steps)


def _dispatch(
    name: str | None, args: dict[str, Any], tools: dict[str, Tool], step: Step
) -> str:
    """Call a whitelisted tool by name; return its result as an observation string.

    Never raises: an unknown tool or a tool exception becomes an error
    observation the model can read and react to (retry a different tool, or give
    up and answer). This keeps a single bad step from crashing the batch — the
    fail-soft discipline the pipeline uses everywhere.
    """
    if name not in tools:
        step.error = f"unknown tool {name!r}; available: {sorted(tools)}"
        return _format_error(step.error)
    try:
        result = tools[name](**args)
    except TypeError as exc:
        # Wrong/missing args for the tool — a model mistake, recoverable.
        step.error = f"bad arguments for {name!r}: {exc}"
        return _format_error(step.error)
    except Exception as exc:  # noqa: BLE001 — fail-soft: surface, don't crash.
        step.error = f"{name!r} failed: {type(exc).__name__}"
        logger.warning("tool %r raised: %s", name, type(exc).__name__)
        return _format_error(step.error)
    return _stringify(result)


def _force_final(
    system, task, transcript, tools, model, generate, log_dir, steps
) -> AgentResult:
    """Last-resort call after ``max_steps``: forbid tools, demand final JSON.

    One extra generation with an appended instruction that the step budget is
    spent and it must answer now using only what it already gathered. If it still
    won't produce parseable final JSON, we return ``final=None`` so the caller
    marks the offer ``needs_review`` — never a fabricated answer.
    """
    prompt = (
        _build_prompt(task, transcript, tools)
        + "\n\nYou have used all your tool steps. Do NOT call any more tools. "
        'Respond NOW with your best final answer as {"final": {...}} JSON only, '
        "using only the information already gathered above."
    )
    gen = generate(model, prompt, system=system, log_dir=log_dir)
    step = Step(index=len(steps) + 1, raw=gen.response)
    action = _parse_action(gen.response)
    if action is not None and "final" in action:
        step.final = action["final"]
        steps.append(step)
        logger.info("[forced] final answer after max_steps")
        return AgentResult(final=action["final"], steps=steps, stopped_reason="forced_final")
    step.error = "no final answer even when forced"
    steps.append(step)
    logger.info("[forced] no parseable final answer — needs_review")
    return AgentResult(final=None, steps=steps, stopped_reason="no_final")


# --------------------------------------------------------------------------
# Prompt assembly + action parsing (plain strings + JSON — no magic)
# --------------------------------------------------------------------------


def _build_prompt(task: str, transcript: list[str], tools: dict[str, Tool]) -> str:
    """Assemble this turn's user prompt: the task + the running transcript.

    The transcript is the agent's entire memory — the model is stateless, so
    everything it "knows" this turn is text we hand it. We list the available
    tool names each turn as a reminder; the *contract* (how to call them, the
    output schema) lives in the system prompt the caller supplies.
    """
    parts = [
        "## Task",
        task.strip(),
        "",
        "## Tools you may call",
        ", ".join(sorted(tools)) or "(none)",
    ]
    if transcript:
        parts += ["", "## Work so far (your previous actions and their results)", *transcript]
    parts += [
        "",
        "## Your next action",
        'Reply with ONE JSON object: either {"tool": "<name>", "args": {...}} to '
        'call a tool, or {"final": {...}} to finish. JSON only, no prose.',
    ]
    return "\n".join(parts)


def _format_turn(model_text: str, observation: str) -> str:
    """One (action, observation) entry appended to the transcript.

    The observation is fenced as untrusted data. The model's own prior text is
    echoed back so the running transcript reads as a coherent dialogue it can
    follow.
    """
    return (
        f"You replied: {model_text.strip()}\n"
        f"{_OBS_OPEN}\n{observation}\n{_OBS_CLOSE}"
    )


def _format_error(message: str) -> str:
    """An error observation — same fenced treatment, prefixed so it's legible."""
    return f"ERROR: {message}"


_FENCE_PREFIX = "```"


def _parse_action(raw: str) -> dict[str, Any] | None:
    """Parse the model's turn into an action dict, or None if unparseable.

    Tolerant like the scorer's ``_extract_json``: local models wrap JSON in
    ```fences``` or a stray sentence. We strip a leading/trailing fence and take
    the outermost ``{...}`` span. Returns the dict for a well-formed
    ``{"tool":...}`` or ``{"final":...}``; None otherwise (the loop then nudges
    the model to re-emit valid JSON — it does not count as a final answer).
    """
    text = raw.strip()
    if text.startswith(_FENCE_PREFIX):
        # Drop a leading ```json / ``` line and any trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith(_FENCE_PREFIX):
            text = text.rstrip()[: -len(_FENCE_PREFIX)]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "final" in data or "tool" in data:
        return data
    return None


def _stringify(result: Any) -> str:
    """Render a tool's return value as an observation string.

    Strings pass through (already human/model-readable — e.g. page text). Other
    JSON-serializable values (a search-results list, a dict) are pretty-printed
    as JSON so the model sees structured fields it can reason over.
    """
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(result)

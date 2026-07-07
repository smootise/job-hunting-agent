"""Ollama client with mandatory full-interaction logging.

Deliberately thin: one function, `generate`, that wraps Ollama's HTTP API.
No retry logic, no streaming, no framework — those get added in the
pipeline stages that need them (e.g. the scoring step's one retry on
invalid JSON). This module's only job is: call the model, log exactly
what went in and what came out, return the raw text.

Why log unconditionally rather than behind a --verbose flag: the whole
point of building this by hand instead of using a framework is to be
able to see what the model actually saw and said. Logs are the mechanism
for that, so they aren't optional.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_LOG_DIR = Path("logs")


@dataclass
class GenerationResult:
    """What a single call to `generate` produced, plus timing."""

    model: str
    prompt: str
    response: str
    duration_seconds: float
    log_path: Path


def generate(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    log_dir: Path = DEFAULT_LOG_DIR,
    timeout_seconds: float = 120.0,
) -> GenerationResult:
    """Send one prompt to `model` via Ollama and log the full exchange.

    Only one model should be resident in VRAM at a time on the 32 GB
    card (see CLAUDE.md's Models section) — Ollama swaps automatically
    when you call a different model name, at the cost of a few seconds'
    load time. That's a non-issue for a nightly batch job.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {"model": model, "prompt": prompt, "stream": False}
    if system is not None:
        payload["system"] = system

    started = time.monotonic()
    with httpx.Client(timeout=timeout_seconds) as client:
        http_response = client.post(ollama_url, json=payload)
        http_response.raise_for_status()
    duration = time.monotonic() - started

    body = http_response.json()
    response_text = body.get("response", "")

    log_path = _write_log(log_dir, model, prompt, system, response_text, duration)

    return GenerationResult(
        model=model,
        prompt=prompt,
        response=response_text,
        duration_seconds=duration,
        log_path=log_path,
    )


def _write_log(
    log_dir: Path,
    model: str,
    prompt: str,
    system: str | None,
    response: str,
    duration_seconds: float,
) -> Path:
    """Write one JSON log file per call: full prompt in, full response out."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_model = model.replace(":", "-").replace("/", "-")
    log_path = log_dir / f"{timestamp}_{safe_model}.json"

    log_path.write_text(
        json.dumps(
            {
                "model": model,
                "system": system,
                "prompt": prompt,
                "response": response,
                "duration_seconds": duration_seconds,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return log_path

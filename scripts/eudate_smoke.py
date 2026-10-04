"""Fail-fast smoke test for static/eudate.js — the dd/mm/yyyy date conversions.

The offer-list date filters are EU-formatted text inputs backed by a hidden ISO
``<input type="date">`` (see ``web/templates/offers/_datefield.html``), because a
native date input's *display* format can't be set by the page — Chrome follows
the document ``lang``, and Firefox/Safari follow the OS locale. The conversion
between ``dd/mm/yyyy`` and ``yyyy-mm-dd`` therefore happens in JavaScript, which
the Python test-suite can't reach.

This script exercises those two pure functions with Node, asserting in particular
that an **impossible date is rejected rather than silently rolled over**
(``31/02`` must NOT become 3 March) and that a US-order typo (``09/21/2026``) is
refused rather than misread — either would quietly filter out real offers, the
project's worst failure mode.

Usage:  uv run python scripts/eudate_smoke.py

Skips with a clear message (exit 0) if Node isn't installed; there's no JS
toolchain in this project and none is required for the app itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_JS = Path(__file__).resolve().parents[1] / "src" / "jobscout" / "web" / "static" / "eudate.js"

# (input, expected) for euToIso. None = must be rejected.
_EU_TO_ISO: list[tuple[str, str | None]] = [
    ("21/09/2026", "2026-09-21"),
    ("4/10/2026", "2026-10-04"),       # single-digit day/month
    ("04.10.2026", "2026-10-04"),      # dot separator
    ("04-10-2026", "2026-10-04"),      # dash separator
    ("  21/09/2026  ", "2026-09-21"),  # trimmed
    ("29/02/2024", "2024-02-29"),      # real leap day
    ("31/02/2026", None),              # must NOT roll over to 03/03
    ("29/02/2026", None),              # not a leap year
    ("00/01/2026", None),
    ("32/01/2026", None),
    ("01/13/2026", None),
    ("09/21/2026", None),              # US order: refuse, don't misread
    ("21/09/26", None),                # 2-digit year
    ("2026-09-21", None),              # ISO typed into the EU box
    ("", None),
    ("abc", None),
]

_ISO_TO_EU: list[tuple[str, str]] = [
    ("2026-09-21", "21/09/2026"),
    ("2026-01-05", "05/01/2026"),
    ("", ""),
    ("garbage", ""),
]

_ROUND_TRIP = ["2026-01-01", "2026-12-31", "2024-02-29", "2026-10-04"]

# Lifts the two pure functions out of the IIFE (brace-matching from their
# `function NAME(` declaration) and runs the cases the harness feeds in on stdin.
_RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function grab(name) {
  const i = src.indexOf("function " + name + "(");
  if (i < 0) throw new Error("missing function: " + name);
  let depth = 0, started = false, j = i;
  for (; j < src.length; j++) {
    if (src[j] === "{") { depth++; started = true; }
    else if (src[j] === "}") { depth--; if (started && depth === 0) { j++; break; } }
  }
  return src.slice(i, j);
}
const fns = new Function(grab("isoToEu") + "\n" + grab("euToIso") +
                         "\nreturn {isoToEu: isoToEu, euToIso: euToIso};")();
const spec = JSON.parse(fs.readFileSync(0, "utf8"));
const out = {
  euToIso: spec.euToIso.map((t) => fns.euToIso(t)),
  isoToEu: spec.isoToEu.map((t) => fns.isoToEu(t)),
  roundTrip: spec.roundTrip.map((iso) => fns.euToIso(fns.isoToEu(iso))),
};
process.stdout.write(JSON.stringify(out));
"""


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("[SKIP] Node not found — eudate.js conversions not checked.")
        return 0
    if not _JS.is_file():
        print(f"[FAIL] missing {_JS}")
        return 1

    spec = {
        "euToIso": [t for t, _ in _EU_TO_ISO],
        "isoToEu": [t for t, _ in _ISO_TO_EU],
        "roundTrip": _ROUND_TRIP,
    }
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.cjs"
        runner.write_text(_RUNNER, encoding="utf-8")
        proc = subprocess.run(
            [node, str(runner), str(_JS)],
            input=json.dumps(spec),
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        print(f"[FAIL] node error:\n{proc.stderr.strip()}")
        return 1

    got = json.loads(proc.stdout)
    failures: list[str] = []

    print("Checking eudate.js date conversions via Node...\n")
    for (text, want), actual in zip(_EU_TO_ISO, got["euToIso"], strict=True):
        ok = actual == want
        label = "rejected" if want is None else want
        print(f"  [{'OK' if ok else 'FAIL'}] euToIso({text!r}) -> {actual!r}"
              f"{'' if ok else f'  (want {label!r})'}")
        if not ok:
            failures.append(f"euToIso({text!r})")

    for (iso, want), actual in zip(_ISO_TO_EU, got["isoToEu"], strict=True):
        ok = actual == want
        print(f"  [{'OK' if ok else 'FAIL'}] isoToEu({iso!r}) -> {actual!r}"
              f"{'' if ok else f'  (want {want!r})'}")
        if not ok:
            failures.append(f"isoToEu({iso!r})")

    for iso, actual in zip(_ROUND_TRIP, got["roundTrip"], strict=True):
        ok = actual == iso
        print(f"  [{'OK' if ok else 'FAIL'}] round trip {iso} -> {actual!r}")
        if not ok:
            failures.append(f"round trip {iso}")

    total = len(_EU_TO_ISO) + len(_ISO_TO_EU) + len(_ROUND_TRIP)
    if failures:
        print(f"\n[FAIL] {len(failures)}/{total} check(s) failed: {', '.join(failures)}")
        return 1
    print(f"\n[OK] All {total} eudate.js checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

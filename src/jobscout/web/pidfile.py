"""A tiny PID-file for the dev server, so `jobscout serve --stop` works.

Why this exists: on Windows a running `jobscout serve` holds a lock on
`.venv/Scripts/jobscout.exe`, which makes `uv sync`/`uv run` fail with "file in
use" when the package needs rebuilding — and stopping the server by hunting for
the process on its port is tedious. So `serve` records its PID on startup and
removes it on exit, and `serve --stop` reads that PID and terminates it.

Deliberately minimal: one file under the project root's `data/` dir (already
gitignored), no locking library, best-effort cleanup. A stale file (from a hard
kill) is detected by checking whether the PID is actually alive.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

_PID_FILENAME = ".jobscout-serve.pid"


def pid_path(project_root: Path) -> Path:
    """Where the serve PID file lives (project ``data/`` — gitignored)."""
    return project_root / "data" / _PID_FILENAME


def write(project_root: Path) -> Path:
    """Record the current process PID; return the file path (for later removal)."""
    path = pid_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def remove(path: Path) -> None:
    """Best-effort delete of the PID file (safe if already gone)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists (cross-platform)."""
    if pid <= 0:
        return False
    try:
        # signal 0 doesn't kill — it just probes existence/permissions.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, we just can't signal it
    except OSError:
        # On Windows os.kill(pid, 0) raises for a missing PID; treat as dead.
        return False
    return True


def stop(project_root: Path) -> str:
    """Stop the server recorded in the PID file. Return a human-readable result.

    Reads the PID, verifies it's alive, then sends a terminate signal
    (``CTRL_BREAK_EVENT`` on Windows — the server handles ``SIGBREAK`` — else
    ``SIGTERM``). Removes the PID file afterward. Never raises for the common
    "nothing running" cases; returns a message the CLI prints.
    """
    path = pid_path(project_root)
    if not path.exists():
        return "No PID file — is a `jobscout serve` running from this project?"

    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        remove(path)
        return "PID file was unreadable; removed it."

    if not _pid_alive(pid):
        remove(path)
        return f"No live process for PID {pid} (stale file removed)."

    try:
        sig = getattr(signal, "SIGBREAK", signal.SIGTERM)
        os.kill(pid, sig)
    except OSError as exc:
        return f"Could not signal PID {pid}: {exc}"

    remove(path)
    return f"Sent stop signal to jobscout serve (PID {pid})."

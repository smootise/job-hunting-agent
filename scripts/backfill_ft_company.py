"""One-off: backfill recoverable company names on existing anonymous FT rows.

Context: many France Travail employers post anonymously (``entreprise.nom``
absent), so those rows were stored with ``company=''``. The adapter now recovers
a name from the offer text for a few of them (``france_travail.recover_company``),
but that only applies to *future* ingests — the idempotent upsert deliberately
never overwrites already-captured content ("first capture is the record", a
Phase 1 invariant).

This script is the sanctioned, narrow exception: it re-runs the *same* recovery
over the offer's stored title/description and, ONLY where a name is found, writes
it to that row's ``company``. It touches nothing else, changes no other field,
and is safe to re-run (idempotent — a row already carrying a company is skipped).
``--dry-run`` reports what it *would* change without writing.

Usage:
  uv run python scripts/backfill_ft_company.py --dry-run
  uv run python scripts/backfill_ft_company.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from jobscout.adapters.france_travail import recover_company  # noqa: E402
from jobscout.storage import db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing.")
    args = ap.parse_args()

    conn = db.connect()
    rows = conn.execute(
        "SELECT id, title, description FROM jobs "
        "WHERE source = 'france_travail' AND (company IS NULL OR TRIM(company) = '')"
    ).fetchall()

    print(f"anonymous FT rows (blank company): {len(rows)}")
    changes: list[tuple[int, str, str]] = []
    for row in rows:
        name = recover_company(row["title"], row["description"])
        if name:
            changes.append((row["id"], name, row["title"]))

    print(f"recoverable: {len(changes)}\n")
    for job_id, name, title in changes:
        print(f"  id={job_id}  company <- {name!r}   ({title[:55]!r})")

    if not changes:
        print("\nnothing to backfill.")
        return

    if args.dry_run:
        print("\n(dry-run — nothing written)")
        return

    for job_id, name, _title in changes:
        conn.execute("UPDATE jobs SET company = ? WHERE id = ?", (name, job_id))
    conn.commit()
    print(f"\nbackfilled {len(changes)} rows.")


if __name__ == "__main__":
    main()

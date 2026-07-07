"""Command-line entry point for the Job Scout pipeline.

Phase 0: this is a stub. Later phases wire in the real pipeline
(ingest -> normalize -> dedupe -> filter -> enrich -> score -> digest);
for now it only proves the package installs and runs.
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jobscout",
        description="Local AI agent pipeline for job hunting.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing to data/ or output/ (not yet implemented).",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("jobscout: dry-run mode (pipeline not yet implemented)")
    else:
        print("jobscout: pipeline not yet implemented")


if __name__ == "__main__":
    main()

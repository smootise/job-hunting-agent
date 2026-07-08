"""Command-line entry point for the Job Scout pipeline.

Phase 1 wires in the first real subcommand, `ingest`: fetch the enabled
sources, dedupe, and persist new offers to data/jobs.db. Later phases add
`filter`, `score`, `digest`, etc. as sibling subcommands on the same parser.

The CLI stays thin on purpose — it parses args, calls `run_ingest`, and prints
a summary. All the logic lives in the pipeline/adapters so it's testable
without spawning a process.
"""

from __future__ import annotations

import argparse
import logging

from jobscout.pipeline import enrich_linkedin, filter_stage, ingest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="jobscout",
        description="Local AI agent pipeline for job hunting.",
    )
    subparsers = parser.add_subparsers(dest="command")

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Fetch sources and store new offers in data/jobs.db.",
    )
    ingest_parser.add_argument(
        "--source",
        action="append",
        choices=list(ingest.SOURCES),
        help="Source(s) to ingest. Repeatable; default = all sources.",
    )
    ingest_parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Per-source hard cap on offers fetched (default: 200).",
    )
    ingest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report counts without writing to data/.",
    )

    filter_parser = subparsers.add_parser(
        "filter",
        help="Apply the preferences.yaml hard filters to stored offers.",
    )
    filter_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max offers to judge this run (default: all pending).",
    )
    filter_parser.add_argument(
        "--refilter",
        action="store_true",
        help="Re-judge every offer, not just un-filtered ones "
        "(use after editing preferences.yaml).",
    )
    filter_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts and log rejections without writing verdicts.",
    )

    enrich_parser = subparsers.add_parser(
        "enrich-linkedin",
        help="Backfill LinkedIn offer descriptions from the public guest "
        "endpoint, then re-filter the enriched rows.",
    )
    enrich_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Max offers to fetch this run (rate-limit cap; default: 25).",
    )
    enrich_parser.add_argument(
        "--min-delay",
        type=float,
        default=2.0,
        help="Minimum seconds between network fetches (default: 2.0).",
    )
    enrich_parser.add_argument(
        "--max-delay",
        type=float,
        default=5.0,
        help="Maximum seconds between network fetches (default: 5.0).",
    )
    enrich_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch/parse and report without writing descriptions or verdicts.",
    )

    args = parser.parse_args(argv)

    if args.command == "ingest":
        _run_ingest(args)
    elif args.command == "filter":
        _run_filter(args)
    elif args.command == "enrich-linkedin":
        _run_enrich_linkedin(args)
    else:
        parser.print_help()


def _run_ingest(args: argparse.Namespace) -> None:
    summary = ingest.run_ingest(
        sources=args.source,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    _print_summary(summary)


def _print_summary(summary: ingest.IngestSummary) -> None:
    """Render a compact per-source table."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    print(f"jobscout ingest{mode}")
    print(f"  {'source':<16} {'fetched':>8} {'new':>6} {'seen_again':>11}  status")
    print(f"  {'-' * 16} {'-' * 8} {'-' * 6} {'-' * 11}  {'-' * 6}")
    for name, outcome in summary.per_source.items():
        if outcome.failed:
            status = f"FAILED: {outcome.error}"
            print(f"  {name:<16} {'-':>8} {'-':>6} {'-':>11}  {status}")
        else:
            new = "-" if summary.dry_run else outcome.new
            seen = "-" if summary.dry_run else outcome.seen_again
            print(
                f"  {name:<16} {outcome.fetched:>8} {str(new):>6} "
                f"{str(seen):>11}  ok"
            )
    if summary.dry_run:
        print(f"  total fetched: {summary.total_fetched} (would-be-new not computed in dry-run)")
    else:
        print(f"  total new: {summary.total_new} / fetched: {summary.total_fetched}")


def _run_filter(args: argparse.Namespace) -> None:
    # Surface the per-rejection log lines the stage emits (transparency: a human
    # should be able to see exactly what got dropped and why).
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = filter_stage.run_filter(
        limit=args.limit,
        refilter=args.refilter,
        dry_run=args.dry_run,
    )
    _print_filter_summary(summary)


def _print_filter_summary(summary: filter_stage.FilterSummary) -> None:
    """Render the passed / needs_review / rejected tally."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    scope = " [refilter: all offers]" if summary.refilter else ""
    print(f"jobscout filter{mode}{scope}")
    print(f"  passed:       {summary.passed}")
    print(f"  needs_review: {summary.needs_review}")
    print(f"  rejected:     {summary.rejected}")
    print(f"  total judged: {summary.total}")
    if summary.total == 0 and not summary.refilter:
        print("  (nothing to filter — all stored offers already judged; "
              "use --refilter to re-judge)")


def _run_enrich_linkedin(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = enrich_linkedin.run_enrich_linkedin(
        limit=args.limit,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        dry_run=args.dry_run,
    )
    _print_enrich_summary(summary)


def _print_enrich_summary(summary: enrich_linkedin.EnrichSummary) -> None:
    """Render the enrichment tally."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    print(f"jobscout enrich-linkedin{mode}")
    print(f"  considered:  {summary.considered}  (LinkedIn offers missing a description)")
    print(f"  enriched:    {summary.enriched}  ({summary.from_cache} from cache)")
    print(f"  failed:      {summary.failed}  (left needs_review, fail-soft)")
    if summary.refiltered:
        changes = ", ".join(f"{k}: {v}" for k, v in sorted(summary.refilter_status_changes.items()))
        print(f"  re-filtered: {summary.refiltered}  -> {changes}")
    if summary.considered == 0:
        print("  (nothing to enrich — all LinkedIn offers already have descriptions)")


if __name__ == "__main__":
    main()

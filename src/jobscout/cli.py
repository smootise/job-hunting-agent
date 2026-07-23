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
import sys

from jobscout.pipeline import (
    enrich_commute,
    enrich_linkedin,
    filter_stage,
    ingest,
    research_address,
    research_company,
    score_stage,
)


def _force_utf8_output() -> None:
    """Make stdout/stderr UTF-8 so summaries + accented names never crash the CLI.

    On Windows the console defaults to a legacy codepage (cp1252), which raises
    ``UnicodeEncodeError`` the moment we print a ``→`` or an accented company name
    (e.g. 'Showroomprivé'). Our summaries and log lines legitimately contain both,
    so we reconfigure the streams to UTF-8 with a safe error handler once at
    startup rather than sprinkling ASCII-only text everywhere. ``reconfigure``
    exists on Python 3.7+ TextIO streams; guard for the rare case it doesn't.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # already-detached or non-reconfigurable stream — best effort.


def main(argv: list[str] | None = None) -> None:
    _force_utf8_output()
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

    commute_parser = subparsers.add_parser(
        "enrich-commute",
        help="Resolve office addresses and compute commute times (Google Routes) "
        "for passed/needs_review offers.",
    )
    commute_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max offers to enrich this run (default: all pending).",
    )
    commute_parser.add_argument(
        "--re-enrich",
        action="store_true",
        help="Re-enrich every enrichable offer, not just un-enriched ones "
        "(use after editing home/commute in preferences.yaml).",
    )
    commute_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and route, report counts, write nothing.",
    )

    research_addr_parser = subparsers.add_parser(
        "research-address",
        help="Run the address-research agent on offers the deterministic chain "
        "can't place (bare 'Paris' / unresolved); store validated IDF addresses.",
    )
    research_addr_parser.add_argument(
        "--limit", type=int, default=None,
        help="Max candidate offers to examine this run (default: all).",
    )
    research_addr_parser.add_argument(
        "--model", default=research_address.DEFAULT_MODEL,
        help=f"Ollama model for the agent (default: {research_address.DEFAULT_MODEL}).",
    )
    research_addr_parser.add_argument(
        "--redo", action="store_true",
        help="Re-offer offers already placed by the agent (retry the search).",
    )
    research_addr_parser.add_argument(
        "--dry-run", action="store_true",
        help="Research and validate, report counts, write no addresses "
        "(LLM-call logs are still written).",
    )

    research_co_parser = subparsers.add_parser(
        "research-company",
        help="Research every passed/needs_review offer's company (WTTJ profile + "
        "web) into a grounded brief that feeds the scorer as advisory context.",
    )
    research_co_parser.add_argument(
        "--limit", type=int, default=None,
        help="Max offers to research this run (default: all pending).",
    )
    research_co_parser.add_argument(
        "--model", default=research_company.DEFAULT_MODEL,
        help=f"Ollama model for the agent (default: {research_company.DEFAULT_MODEL}).",
    )
    research_co_parser.add_argument(
        "--redo", action="store_true",
        help="Re-research every offer, not just un-researched ones.",
    )
    research_co_parser.add_argument(
        "--dry-run", action="store_true",
        help="Research and ground, report counts, write no briefs "
        "(LLM-call logs are still written).",
    )

    score_parser = subparsers.add_parser(
        "score",
        help="Score passed/needs_review offers against the preferences.yaml "
        "rubric with the local LLM (zero tools); blends the Python commute "
        "sub-score into a weighted 0-100 total.",
    )
    score_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max offers to score this run (default: all pending).",
    )
    score_parser.add_argument(
        "--model",
        default=score_stage.DEFAULT_MODEL,
        help=f"Ollama model to score with (default: {score_stage.DEFAULT_MODEL}).",
    )
    score_parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-score every scoreable offer, not just un-scored ones "
        "(use after editing the rubric or once an address resolves).",
    )
    score_parser.add_argument(
        "--commute-only",
        action="store_true",
        help="Recompute only weekly_commute_fit + the total from each offer's "
        "existing score (no LLM call); use after a commute changes. Preserves "
        "the LLM's qualitative scores and reasoning.",
    )
    score_parser.add_argument(
        "--ids",
        type=int,
        nargs="+",
        default=None,
        metavar="ID",
        help="Score only these job ids (still gated on eligibility). Implies "
        "'score these' — overrides the default unscored-only selection.",
    )
    score_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Call the model and report totals, but write no scores "
        "(the full LLM-call logs are still written).",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Launch the read-only webapp (dashboard + offer list + detail) "
        "over data/jobs.db. Triggers nothing — it only browses existing data.",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — a local, single-user tool; "
        "do not expose on 0.0.0.0).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8020,
        help="Port to serve on (default: 8020).",
    )
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (development).",
    )
    serve_parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop a running `jobscout serve` (via its PID file) and exit. "
        "Frees the port and the lock on jobscout.exe so `uv` can rebuild.",
    )

    args = parser.parse_args(argv)

    if args.command == "ingest":
        _run_ingest(args)
    elif args.command == "filter":
        _run_filter(args)
    elif args.command == "enrich-linkedin":
        _run_enrich_linkedin(args)
    elif args.command == "enrich-commute":
        _run_enrich_commute(args)
    elif args.command == "research-address":
        _run_research_address(args)
    elif args.command == "research-company":
        _run_research_company(args)
    elif args.command == "score":
        _run_score(args)
    elif args.command == "serve":
        _run_serve(args)
    else:
        parser.print_help()


def _run_serve(args: argparse.Namespace) -> None:
    """Launch the webapp via uvicorn, making Ctrl+C stop everything cleanly.

    Imported lazily so the web dependencies (fastapi/uvicorn/jinja2) are only
    needed by anyone who runs ``serve`` — the pipeline commands don't import
    them. We always hand uvicorn the *import string* ``"jobscout.web.app:app"``
    (never a pre-built instance) so the reloader and the server share one target;
    the module-level ``app`` reads its data paths from ``resolve_settings``
    (project root + env overrides), so only the socket bind is passed here.

    **Shutdown.** ``--reload`` uses uvicorn's own supervisor, whose child
    handles Ctrl+C. The plain path, though, has a known Windows quirk: uvicorn's
    default SIGINT handling can miss a Ctrl+C when the event loop is idle (no
    live connection to wake it), leaving an orphaned worker on the port. So for
    the plain path we drive a ``uvicorn.Server`` ourselves and install our own
    SIGINT/SIGTERM handler that flips ``server.should_exit`` — deterministic on
    Windows and POSIX alike. A short ``timeout_graceful_shutdown`` guarantees the
    process actually exits instead of hanging on a slow connection.
    """
    import os
    import signal

    import uvicorn

    from jobscout.web import pidfile
    from jobscout.web.settings import resolve_settings

    settings = resolve_settings(host=args.host, port=args.port)

    # `--stop`: kill a server started earlier from this project (via its PID
    # file), then exit. Frees the port and the lock on jobscout.exe so `uv` can
    # rebuild — the recurring Windows friction this is here to remove.
    if args.stop:
        print(pidfile.stop(settings.project_root))
        return

    # The server runs the module-level ``jobscout.web.app:app`` (import string),
    # which builds its Settings from the environment — so the chosen host/port
    # must be exported BEFORE that import, or the app's Settings (hence the
    # same-origin guard) would keep the defaults and reject requests to a
    # non-default port.
    os.environ["JOBSCOUT_HOST"] = args.host
    os.environ["JOBSCOUT_PORT"] = str(args.port)

    print(f"Job Scout webapp → http://{args.host}:{args.port}  (db: {settings.db_path})")
    print("Press Ctrl+C to stop (or `jobscout serve --stop` from another shell).")

    if args.reload:
        # The reloader supervises a child process and forwards Ctrl+C to it;
        # its default signal handling is reliable, so use the stock runner. (No
        # PID file here — the reloader's child PID isn't this process, so --stop
        # wouldn't target it reliably; use Ctrl+C in the reloader's shell.)
        uvicorn.run(
            "jobscout.web.app:app",
            host=args.host,
            port=args.port,
            reload=True,
        )
        return

    config = uvicorn.Config(
        "jobscout.web.app:app",
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)

    def _request_stop(signum, frame):  # noqa: ARG001 — signal handler signature
        server.should_exit = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    # SIGBREAK exists only on Windows; it's what a console delivers to a child
    # started in its own process group (Ctrl+Break, and Ctrl+C in that setup),
    # and what `serve --stop` sends. Handling it makes shutdown reliable however
    # the process was spawned.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _request_stop)

    pid_file = pidfile.write(settings.project_root)
    try:
        server.run()
    finally:
        pidfile.remove(pid_file)


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


def _run_enrich_commute(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = enrich_commute.run_enrich_commute(
        limit=args.limit,
        re_enrich=args.re_enrich,
        dry_run=args.dry_run,
    )
    _print_enrich_commute_summary(summary)


def _print_enrich_commute_summary(summary: enrich_commute.EnrichCommuteSummary) -> None:
    """Render the address/commute enrichment tally."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    scope = " [re-enrich: all enrichable]" if summary.re_enrich else ""
    print(f"jobscout enrich-commute{mode}{scope}")
    print(f"  considered:     {summary.considered}  (passed/needs_review, not yet enriched)")
    print(f"  enriched:       {summary.enriched}  ({summary.remote_skipped} remote → commute 0, "
          f"{summary.approximate} on approximate address)")
    print(f"  needs address:  {summary.needs_address}  (too vague to route, e.g. bare "
          f"'Paris'; flagged for the address agent)")
    print(f"  failed:         {summary.failed}  (unresolved address or routing failure; "
          f"left for a later run)")
    if summary.considered == 0 and not summary.re_enrich:
        print("  (nothing to enrich — all passed/needs_review offers already enriched; "
              "use --re-enrich to redo)")


def _run_research_address(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = research_address.run_research_address(
        model=args.model, limit=args.limit, redo=args.redo, dry_run=args.dry_run,
    )
    _print_research_address_summary(summary)


def _print_research_address_summary(summary: research_address.ResearchAddressSummary) -> None:
    """Render the address-research tally."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    scope = " [redo: all]" if summary.redo else ""
    print(f"jobscout research-address{mode}{scope}")
    print(f"  considered:   {summary.considered}  (passed/needs_review candidate rows)")
    print(f"  needed agent: {summary.needed_agent}  (deterministic chain couldn't place)")
    print(f"  resolved:     {summary.resolved}  (agent address passed IDF validation)")
    print(f"  unresolved:   {summary.unresolved}  (no valid address; centroid fallback stands)")
    if summary.considered == 0:
        print("  (nothing to research — no passed/needs_review offers)")


def _run_research_company(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = research_company.run_research_company(
        model=args.model, limit=args.limit, redo=args.redo, dry_run=args.dry_run,
    )
    _print_research_company_summary(summary)


def _print_research_company_summary(summary: research_company.ResearchCompanySummary) -> None:
    """Render the company-research tally."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    scope = " [redo: all]" if summary.redo else ""
    print(f"jobscout research-company{mode}{scope}")
    print(f"  considered:   {summary.considered}  (passed/needs_review, not yet researched)")
    print(f"  briefed:      {summary.briefed}  ({summary.from_wttj} had a WTTJ profile)")
    print(f"  needs_review: {summary.needs_review}  (nothing grounded / agent error)")
    print(f"  skipped:      {summary.skipped_no_company}  (anonymous offers — no company name to research)")
    if summary.considered == 0 and not summary.redo:
        print("  (nothing to research — all passed/needs_review offers already researched; "
              "use --redo to redo)")


def _run_score(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = score_stage.run_score(
        model=args.model,
        limit=args.limit,
        rescore=args.rescore,
        commute_only=args.commute_only,
        ids=args.ids,
        dry_run=args.dry_run,
    )
    _print_score_summary(summary)


def _print_score_summary(summary: score_stage.ScoreSummary) -> None:
    """Render the scoring tally."""
    mode = " (dry-run - nothing written)" if summary.dry_run else ""
    if summary.commute_only:
        scope = " [commute-only: recompute from stored score, no LLM]"
    elif summary.rescore:
        scope = " [rescore: all scoreable]"
    else:
        scope = ""
    print(f"jobscout score{mode}{scope}")
    print(f"  considered:   {summary.considered}")
    print(f"  scored:       {summary.scored}  ({summary.commute_unknown} with commute unknown)")
    if summary.commute_only:
        print(f"  skipped:      {summary.skipped}  (no prior score to recompute)")
    else:
        print(f"  needs_review: {summary.needs_review}  (invalid JSON x2 or model error)")
    if summary.considered == 0 and not (summary.rescore or summary.commute_only):
        print("  (nothing to score — all passed/needs_review offers already scored; "
              "use --rescore to redo)")


if __name__ == "__main__":
    main()

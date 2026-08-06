"""The webapp's routes: read views (dashboard, list, detail) + V2 write actions.

The list page and its HTMX fragment share one ``_table.html`` partial, so the
first full-page paint and every subsequent sort/filter swap render identically.

V2 adds the app's first **write** routes: setting an offer's review disposition +
notes, and triggering pipeline stages via the background runner. All writes go
through ``require_local_origin`` (a same-origin guard) and the runner runs the
same pipeline code the CLI does — no new external actions, no email.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from jobscout.storage import db, queries, stats

from . import runner as runner_module
from .dependencies import get_conn
from .runner import RunnerBusy
from .security import require_local_origin

router = APIRouter()

# The offer sources, for the list-page filter dropdown.
_SOURCES = ("wttj", "france_travail", "linkedin_email")

# Score-status filter options (value → label) for the list dropdown.
_SCORE_STATUSES = (
    ("scored", "Scored"),
    ("needs_review", "Score needs review"),
)

# The "active" offers (not yet rejected) — the default list view. The Status
# control adds an explicit "all" (incl. rejected) and single-status options.
_ACTIVE_STATUSES = ("passed", "needs_review")

# Sentinel status-filter value meaning "show everything, including rejected" —
# distinct from the default (no param → active only) and from a single status.
_STATUS_ALL = "all"

# How far back the "posted on/after" picker defaults to, so old (likely closed)
# postings are hidden on first load without a manual pick.
_DEFAULT_POSTED_WINDOW = _dt.timedelta(days=30)


def _default_posted_after() -> str:
    """The default 'posted on/after' bound: today minus the window, ISO date."""
    return (_dt.date.today() - _DEFAULT_POSTED_WINDOW).isoformat()


def _validate_iso_date(value: str) -> str:
    """Return ``value`` if it's a valid ISO ``YYYY-MM-DD`` date, else raise 400.

    The date arrives from an HTTP query string and is compared against
    ``posted_at`` in SQL. Though ``list_jobs`` parameterizes it (so it's not an
    injection vector), we still reject a malformed value loudly rather than let a
    garbage bound silently match nothing — same 'no silent drift' stance as the
    sort guard.
    """
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {value!r}") from exc
    return value

# The disposition options offered in the list filter (value → label). The three
# real dispositions plus the "unreviewed" sentinel (queries.UNREVIEWED).
_DISPOSITION_FILTERS = (
    (queries.UNREVIEWED, "Unreviewed"),
    ("to_review", "To review"),
    ("applied", "Applied"),
    ("not_interested", "Not interested"),
)


def _templates(request: Request):
    return request.app.state.templates


def _criteria(request: Request) -> list[dict]:
    """The rubric criteria (name/weight/description) cached on app.state."""
    return request.app.state.criteria


def _criteria_names(request: Request) -> frozenset[str]:
    return request.app.state.criteria_names


@router.get("/healthz")
def healthz() -> JSONResponse:
    """Trivial liveness probe for smoke tests / uptime checks."""
    return JSONResponse({"ok": True})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    """The overview: agent-pipeline funnel + pending backlog + last run +
    the owner's review counts + the run-controls panel."""
    return _templates(request).TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats.dashboard_stats(conn),
            "review": stats.review_stats(conn),
            "last_run": stats.last_run(conn),
            "recent_runs": stats.recent_runs(conn, limit=8),
            "runner_state": request.app.state.runner.snapshot(),
            "run_stages": runner_module.PIPELINE_ORDER,
        },
    )


def _list_context(request: Request, conn: sqlite3.Connection) -> dict:
    """Shared context for the list page and its HTMX table fragment.

    Reads the sort/filter query params, validates the sort against the rubric +
    allowlist (a bad ``sort`` is a client error, surfaced as 400 rather than a
    silent fallback so the UI can't drift into an un-sorted state unnoticed),
    and hydrates the rows so the template reads parsed JSON.
    """
    params = request.query_params
    source = params.get("source") or None
    score_status = params.get("score_status") or None
    disposition = params.get("disposition") or None
    order_by = params.get("sort") or "score_total"
    descending = params.get("dir", "desc") != "asc"

    # Status: the dropdown submits "" (default → active only, hide rejected),
    # "all" (everything incl. rejected), or a single status. Absent == "" == default.
    status_sel = params.get("filter_status") or ""
    if status_sel == _STATUS_ALL:
        filter_status, statuses = None, None            # no status filter
    elif status_sel in ("passed", "needs_review", "rejected"):
        filter_status, statuses = status_sel, None      # one status
    else:
        filter_status, statuses = None, _ACTIVE_STATUSES  # default: active only

    # Date: absent → 30-day default; present-but-empty → user cleared it (no bound);
    # present with a value → validate. "hide_undated" checkbox flips include_undated.
    raw_posted = params.get("posted_after")
    if raw_posted is None:
        posted_after = _default_posted_after()
    elif raw_posted == "":
        posted_after = None
    else:
        posted_after = _validate_iso_date(raw_posted)
    hide_undated = params.get("hide_undated") in ("on", "true", "1")

    try:
        rows = queries.list_jobs(
            conn,
            filter_status=filter_status,
            statuses=statuses,
            source=source,
            score_status=score_status,
            disposition=disposition,
            posted_after=posted_after,
            include_undated=not hide_undated,
            order_by=order_by,
            descending=descending,
            criteria_names=_criteria_names(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Whether any control departs from the default view (drives a "Clear filters"
    # affordance + makes a narrow view unmistakable for an empty DB).
    is_default = (
        status_sel == "" and source is None and score_status is None
        and disposition is None and raw_posted is None and not hide_undated
    )

    return {
        "jobs": [queries.hydrate_job(r) for r in rows],
        "criteria": _criteria(request),
        "sources": _SOURCES,
        "disposition_filters": _DISPOSITION_FILTERS,
        "score_statuses": _SCORE_STATUSES,
        "is_default_view": is_default,
        # Echo the active controls back so the form/links stay in sync.
        "active": {
            "filter_status": status_sel,
            "source": source,
            "score_status": score_status,
            "disposition": disposition,
            "posted_after": posted_after or "",
            "hide_undated": hide_undated,
            "sort": order_by,
            "dir": "asc" if not descending else "desc",
        },
    }


@router.get("/offers", response_class=HTMLResponse)
def offers_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    """Full offer-list page (filter form + the shared table partial)."""
    return _templates(request).TemplateResponse(
        request, "offers/list.html", _list_context(request, conn)
    )


@router.get("/offers/table", response_class=HTMLResponse)
def offers_table(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    """Just the ``<table>`` — the HTMX target for sort/filter partial swaps."""
    return _templates(request).TemplateResponse(
        request, "offers/_table.html", _list_context(request, conn)
    )


@router.get("/offers/{job_id}", response_class=HTMLResponse)
def offer_detail(
    request: Request, job_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    """One offer: verdict, full score breakdown, commute detail, company + posting."""
    row = queries.get_job(conn, job_id)
    if row is None:
        return _templates(request).TemplateResponse(
            request, "errors/404.html", {"job_id": job_id}, status_code=404
        )
    return _templates(request).TemplateResponse(
        request,
        "offers/detail.html",
        {
            "job": queries.hydrate_job(row),
            "criteria": _criteria(request),
            "runner_state": request.app.state.runner.snapshot(),
            "stats": stats.dashboard_stats(conn),
        },
    )


# --------------------------------------------------------------------------
# V2 write routes — review disposition/notes + background run triggers
# --------------------------------------------------------------------------


def _run_status_context(request: Request, conn: sqlite3.Connection) -> dict:
    """Context for the run-status fragment: live runner state + pending counts."""
    return {
        "runner_state": request.app.state.runner.snapshot(),
        "stats": stats.dashboard_stats(conn),
    }


@router.post(
    "/offers/{job_id}/review",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_origin)],
)
def set_review(
    request: Request,
    job_id: int,
    disposition: str = Form(...),
    notes: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Set the owner's disposition + notes for one offer; swap back the control."""
    if queries.get_job(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="offer not found")
    try:
        db.upsert_review(conn, job_id, disposition=disposition, notes=notes.strip() or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()

    row = queries.get_job(conn, job_id)  # re-read with the fresh review row
    return _templates(request).TemplateResponse(
        request, "offers/_review_control.html", {"job": queries.hydrate_job(row)}
    )


@router.post(
    "/runs/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_origin)],
)
def cancel_run(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    """Request cancellation of the active run; return the status fragment.

    Declared BEFORE ``/runs/{stage}`` so "cancel" isn't captured as a stage name.
    Cooperative: the running stage stops after its current offer commits, then the
    chain aborts. Re-running resumes for free. A no-op if nothing is running.
    """
    request.app.state.runner.cancel()
    return _templates(request).TemplateResponse(
        request, "runs/_status.html", _run_status_context(request, conn)
    )


@router.post(
    "/runs/{stage}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_origin)],
)
def trigger_run(
    request: Request, stage: str, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    """Enqueue a pipeline stage (or ``pipeline``); return the status fragment."""
    runner = request.app.state.runner
    try:
        runner.enqueue(stage)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunnerBusy:
        pass  # already running — the fragment shows the current job
    return _templates(request).TemplateResponse(
        request, "runs/_status.html", _run_status_context(request, conn)
    )


# Stages that can run on a single offer (everything except ingest, which fetches
# new offers from sources and has no per-offer form).
_PER_OFFER_STAGES = frozenset(runner_module.PIPELINE_ORDER) - {"ingest"} | {"score-commute-only"}


@router.post(
    "/offers/{job_id}/runs/{stage}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_origin)],
)
def trigger_offer_run(
    request: Request, job_id: int, stage: str, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    """Enqueue a single stage on ONE offer (``<stage> --ids <job_id>``).

    Forces the stage past its 'already done' gate so a deliberate re-run works
    (re-search an address, re-route a commute after a prefs change). The stage's
    own eligibility gate still applies — a rejected offer selects nothing for the
    pipeline stages and the run is a harmless no-op.
    """
    if stage not in _PER_OFFER_STAGES:
        raise HTTPException(status_code=404, detail=f"no per-offer stage {stage!r}")
    if queries.get_job(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="offer not found")
    runner = request.app.state.runner
    try:
        runner.enqueue(stage, job_id=job_id)
    except RunnerBusy:
        pass
    return _templates(request).TemplateResponse(
        request, "runs/_status.html", _run_status_context(request, conn)
    )


@router.get("/runs/status", response_class=HTMLResponse)
def run_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    """The self-polling run-status fragment (progress bar / last summary)."""
    return _templates(request).TemplateResponse(
        request, "runs/_status.html", _run_status_context(request, conn)
    )

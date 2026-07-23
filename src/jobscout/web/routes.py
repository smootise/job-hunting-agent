"""The webapp's routes: read views (dashboard, list, detail) + V2 write actions.

The list page and its HTMX fragment share one ``_table.html`` partial, so the
first full-page paint and every subsequent sort/filter swap render identically.

V2 adds the app's first **write** routes: setting an offer's review disposition +
notes, and triggering pipeline stages via the background runner. All writes go
through ``require_local_origin`` (a same-origin guard) and the runner runs the
same pipeline code the CLI does — no new external actions, no email.
"""

from __future__ import annotations

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
    filter_status = params.get("filter_status") or None
    source = params.get("source") or None
    disposition = params.get("disposition") or None
    order_by = params.get("sort") or "score_total"
    descending = params.get("dir", "desc") != "asc"

    try:
        rows = queries.list_jobs(
            conn,
            filter_status=filter_status,
            source=source,
            disposition=disposition,
            order_by=order_by,
            descending=descending,
            criteria_names=_criteria_names(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "jobs": [queries.hydrate_job(r) for r in rows],
        "criteria": _criteria(request),
        "sources": _SOURCES,
        "disposition_filters": _DISPOSITION_FILTERS,
        # Echo the active controls back so the form/links stay in sync.
        "active": {
            "filter_status": filter_status,
            "source": source,
            "disposition": disposition,
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


@router.post(
    "/offers/{job_id}/rescore",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_origin)],
)
def trigger_rescore(
    request: Request, job_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> HTMLResponse:
    """Enqueue a targeted re-score (``score --ids <job_id>``) for one offer."""
    if queries.get_job(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="offer not found")
    runner = request.app.state.runner
    try:
        runner.enqueue("rescore", job_id=job_id)
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

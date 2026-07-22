"""The five read-only routes: dashboard, offer list (+ HTMX fragment), detail,
and a health check.

The list page and its HTMX fragment share one ``_table.html`` partial, so the
first full-page paint and every subsequent sort/filter swap render identically.
Everything is GET; there is no path here that writes, triggers a pipeline stage,
or calls an external service.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from jobscout.storage import queries, stats

from .dependencies import get_conn

router = APIRouter()

# The offer sources, for the list-page filter dropdown.
_SOURCES = ("wttj", "france_travail", "linkedin_email")


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
    """The overview: agent-pipeline funnel + pending backlog + last run."""
    return _templates(request).TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats.dashboard_stats(conn),
            "last_run": stats.last_run(conn),
            "recent_runs": stats.recent_runs(conn, limit=8),
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
    order_by = params.get("sort") or "score_total"
    descending = params.get("dir", "desc") != "asc"

    try:
        rows = queries.list_jobs(
            conn,
            filter_status=filter_status,
            source=source,
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
        # Echo the active controls back so the form/links stay in sync.
        "active": {
            "filter_status": filter_status,
            "source": source,
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
        {"job": queries.hydrate_job(row), "criteria": _criteria(request)},
    )

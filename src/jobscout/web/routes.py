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
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import QueryParams

from jobscout.storage import db, queries, stats

from . import runner as runner_module
from . import viewstate
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

# Hard-filter verdicts, as checkbox options (value → label).
_FILTER_STATUSES = (
    ("passed", "Passed"),
    ("needs_review", "Needs review"),
    ("rejected", "Rejected"),
)

# The "active" offers (not yet rejected) — what Status is pre-checked with, so
# the default view still hides rejected offers. Unlike the other filters (where
# no selection means "all"), Status starts with a non-empty selection; the
# "filter_status_set" marker param tells a deliberate clear from a first visit.
_ACTIVE_STATUSES = ("passed", "needs_review")

# How far back the "posted on/after" picker defaults to, so old (likely closed)
# postings are hidden on first load without a manual pick.
_DEFAULT_POSTED_WINDOW = _dt.timedelta(days=30)


def _default_posted_after() -> str:
    """The default 'posted on/after' bound: today minus the window, ISO date."""
    return (_dt.date.today() - _DEFAULT_POSTED_WINDOW).isoformat()


def _validate_choices(values: list[str], allowed: Sequence[str], field: str) -> list[str]:
    """Keep the non-empty ``values``, rejecting any outside ``allowed`` with a 400.

    A ``<select multiple>`` submits its field once per chosen option (and an
    unchosen one not at all); the blank "All" option submits ``""``, which we drop
    so it reads as "no filter". Values reaching SQL are always bound, never
    interpolated — this guard is about catching a UI/server mismatch loudly
    instead of returning a confusing empty list, the same stance as the sort guard.
    """
    picked = [v for v in values if v]
    unknown = [v for v in picked if v not in allowed]
    if unknown:
        raise HTTPException(status_code=400, detail=f"invalid {field}: {unknown[0]!r}")
    return picked


def _validate_iso_date(value: str) -> str:
    """Return ``value`` if it's a valid ISO ``YYYY-MM-DD`` date, else raise 400.

    The date arrives from an HTTP query string and is compared against
    ``posted_at`` / ``first_seen_at`` in SQL. Though ``list_jobs`` parameterizes it
    (so it's not an injection vector), we still reject a malformed value loudly
    rather than let a garbage bound silently match nothing — same 'no silent
    drift' stance as the sort guard.
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


def _resolve_view_params(request: Request) -> tuple[QueryParams, bool]:
    """Return the query params to render from, and whether they were restored.

    The list view's state lives entirely in the URL, so leaving the page and
    coming back via the nav would otherwise drop every filter. Three cases:

    * ``?reset=1`` → forget the saved view and render the defaults. The escape
      hatch: a saved filter can always be cleared from the UI.
    * any recognized filter param present → honor the URL and *save* it as the
      new remembered view. An explicit URL always wins over memory, which keeps
      shared/bookmarked links meaning exactly what they say.
    * a bare ``/offers`` → replay the saved view, if there is one.

    Restoration is reported to the caller (not applied silently) so the page can
    say so: a forgotten filter that hides offers is precisely the kind of silent
    failure this project designs against.
    """
    params = request.query_params
    root = request.app.state.settings.project_root

    if params.get("reset") in ("1", "on", "true"):
        viewstate.clear_view(root)
        return QueryParams(""), False

    explicit = [(k, v) for k, v in params.multi_items()
                if k in viewstate.REMEMBERED_PARAMS]
    if explicit:
        viewstate.save_view(root, explicit)
        return params, False

    # Nothing view-shaped in the URL: fall back to the remembered view. Other
    # params already in the URL (e.g. the `new=1` preset) are preserved, and win,
    # since they're merged in after the saved pairs.
    saved = viewstate.load_view(root)
    if not saved:
        return params, False
    merged = saved + list(params.multi_items())
    return QueryParams(merged), True


def _list_context(request: Request, conn: sqlite3.Connection) -> dict:
    """Shared context for the list page and its HTMX table fragment.

    Reads the sort/filter query params, validates the sort against the rubric +
    allowlist (a bad ``sort`` is a client error, surfaced as 400 rather than a
    silent fallback so the UI can't drift into an un-sorted state unnoticed),
    and hydrates the rows so the template reads parsed JSON.

    Params come from ``_resolve_view_params``, which may substitute the saved
    view for a bare ``/offers`` — so everything below parses one params object
    without caring whether it came from the URL or from memory.
    """
    params, restored = _resolve_view_params(request)
    # Multi-select filters: a <select multiple> submits the field once per chosen
    # option, so read every value. Each is checked against its known option set —
    # these end up in SQL ``IN`` clauses (parameterized, so not an injection
    # vector), but an unknown value still means the UI and server disagree, and we
    # surface that as 400 rather than silently matching nothing. An empty
    # selection means "no filter", matching the old ""-means-all behaviour.
    source = _validate_choices(params.getlist("source"), _SOURCES, "source")
    score_status = _validate_choices(
        params.getlist("score_status"), [v for v, _ in _SCORE_STATUSES], "score_status"
    )
    disposition = _validate_choices(
        params.getlist("disposition"), [v for v, _ in _DISPOSITION_FILTERS], "disposition"
    )
    order_by = params.get("sort") or "score_total"
    descending = params.get("dir", "desc") != "asc"

    # Status is the one filter whose default is a non-empty selection (active
    # only, i.e. rejected hidden). That makes an absent param ambiguous: a first
    # visit, or the user unchecking every box? The form always submits the hidden
    # "filter_status_set" marker, so its presence means "this selection is
    # deliberate" — absent → pre-check the active statuses; present but empty →
    # the user cleared it, which we read as "all" (showing nothing would be a
    # useless view, and it matches how the other filters treat empty).
    statuses = _validate_choices(
        params.getlist("filter_status"), [v for v, _ in _FILTER_STATUSES], "filter_status"
    )
    status_touched = "filter_status_set" in params
    if not statuses and not status_touched:
        statuses = list(_ACTIVE_STATUSES)

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

    # Ingestion date ("added on/after"), the counterpart to posted_after. Absent
    # means no bound — unlike posted_after there's no default window, since the
    # whole point is to answer "what's new" on demand.
    raw_seen = params.get("seen_after")
    seen_after = _validate_iso_date(raw_seen) if raw_seen else None

    # The "New (last ingest)" preset: pin seen_after to the last ingest run's
    # start, and CLEAR posted_after so the two date bounds can't silently
    # intersect (a 30-day posted default would hide freshly-ingested-but-old
    # postings, i.e. exactly the offers this view exists to show). The underlying
    # controls stay independently settable; this only prefills them.
    last_ingest = queries.last_ingest_run(conn)
    show_new = params.get("new") in ("1", "on", "true")
    if show_new and last_ingest is not None:
        seen_after = last_ingest["started_at"]
        posted_after = None

    try:
        rows = queries.list_jobs(
            conn,
            statuses=statuses,
            source=source,
            score_status=score_status,
            disposition=disposition,
            posted_after=posted_after,
            seen_after=seen_after,
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
        not status_touched and not source and not score_status
        and not disposition and raw_posted is None and not hide_undated
        and raw_seen is None and not show_new
    )

    return {
        "jobs": [queries.hydrate_job(r) for r in rows],
        "criteria": _criteria(request),
        # Paired (value, label) for the checkbox-dropdown macro; a source's value
        # is its own label.
        "sources": [(s, s) for s in _SOURCES],
        "filter_statuses": _FILTER_STATUSES,
        "disposition_filters": _DISPOSITION_FILTERS,
        "score_statuses": _SCORE_STATUSES,
        "is_default_view": is_default,
        # True when these filters came from the saved view rather than the URL —
        # the page says so, so a narrow result is never mistaken for an empty DB.
        "restored_view": restored,
        # The last ingest run drives the "New" shortcut button and the per-row
        # "new" pill (a row is new when its first_seen_at is at/after that run's
        # start). None when the ledger holds no ingest run yet.
        "last_ingest": last_ingest,
        "showing_new": show_new,
        # Echo the active controls back so the form/links stay in sync.
        "active": {
            "filter_status": statuses,
            "source": source,
            "score_status": score_status,
            "disposition": disposition,
            "posted_after": posted_after or "",
            # Echoed back into a <input type="date">, which only accepts
            # YYYY-MM-DD — so truncate the run timestamp the preset may have set.
            "seen_after": (seen_after or "")[:10],
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

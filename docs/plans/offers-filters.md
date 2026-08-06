# Plan: offer-list date filter + filter/sort improvements

## Motivation
Old postings (likely closed) are cluttering the list. Add a "posted on/after"
date filter, and improve the surrounding filters/sorts while we're in here.

## Data reality (measured, drives the design)
- `posted_at`: 100% populated for WTTJ + France Travail, **0% for LinkedIn**
  (228/638 offers — alert emails carry no post date). Mixed ISO formats
  (`+02:00`, `Z`) but all ISO-8601, so lexical comparison sorts correctly.
- `first_seen_at`: 100% populated (pipeline ingest time) — the reliable "new to
  me" signal, distinct from "posted recently".

## Decisions (owner-confirmed)
- **Undated offers + date filter:** show undated offers by default; add a
  **"hide undated"** checkbox for the stricter view. A date filter must NEVER
  silently drop the 228 LinkedIn offers (project's core no-silent-drop rule).
- **Rejected default:** `/offers` defaults to **hiding rejected** (passed +
  needs_review). The Status control still surfaces rejected on demand.

## Changes

### queries.list_jobs
- New params: `posted_after: str | None`, `include_undated: bool = True`,
  `statuses: tuple[str,...] | None` (multi-status IN-filter, replaces the single
  `filter_status` for the "active-only" default; keep `filter_status` too or
  fold it in — implement as an `IN (?,?)` when a set is passed).
- `posted_after` → `jobs.posted_at >= ?`; when `include_undated`, OR
  `jobs.posted_at IS NULL`. Parameterized (date is untrusted).
- Expose `first_seen_at` + `title` sorts (already in `ALLOWED_SORT_COLUMNS`,
  just unexposed in the UI — no query change needed).

### routes (`_list_context`)
- Read `posted_after` (validate it's an ISO date or empty → else 400, same
  strictness as the sort guard), `hide_undated` (checkbox → bool), and a Status
  model that distinguishes three cases:
  - **default (no `status` param):** active-only → `statuses=("passed","needs_review")`
  - **explicit "all":** a sentinel value → no status filter (incl. rejected)
  - **explicit single:** passed | needs_review | rejected
- Echo all new controls back in `active` so the picker/checkbox/dropdown persist
  across HTMX swaps and a full reload of the pushed URL reproduces the view.
- Surface `score_status` filter (param already supported by `list_jobs`).

### templates/offers/list.html
- `<input type="date" name="posted_after">` + a "hide undated" checkbox (with a
  one-line note: "LinkedIn offers have no post date; shown unless hidden").
- Status dropdown gains an explicit **"All (incl. rejected)"** option distinct
  from the active-only default.
- Sort dropdown gains **"Recently ingested" (first_seen_at)** and **"Title"**.
- A **Clear filters** link + rely on `_table.html`'s existing count so a narrow
  view is never mistaken for an empty DB.

### tests
- `test_queries.py`: `posted_after` filters WTTJ/FT correctly; `include_undated`
  toggles the 228 undated rows in/out; default status hides rejected; explicit
  "all" shows it; a bad `posted_after` raises.
- `webapp_smoke.py`: drive `/offers/table?posted_after=...&hide_undated=on`,
  assert 200 + that an undated offer appears/disappears as expected.

## Non-goals
- No new date *column* or backfilling LinkedIn post dates (not available).
- No change to the pipeline or ingest.

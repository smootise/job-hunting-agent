# Dev workflow

The practical "how to work in this repo" notes — the things you (or a fresh
Claude Code context) shouldn't have to re-derive. Design rationale lives in
`docs/*.md` and `CLAUDE.md`; this is just the mechanics.

## Setup

```
uv sync                                          # install deps into .venv
cp preferences.example.yaml preferences.yaml     # then edit (gitignored: home addr)
cp .env.example .env                             # then fill credentials
```

Python 3.12+, managed with `uv`. Everything runs through `uv run …`.

## Tests + lint

```
uv run pytest                 # full suite — all offline (no Ollama, no network)
uv run pytest tests/test_web.py -q
uv run ruff check src tests
```

Tests run against fixtures + temp SQLite DBs; they never touch `data/jobs.db` or
hit external services (clients/`generate` are injected). Web-route tests use a
FastAPI `TestClient`; the write/run-route tests use the **context-manager** form
(`with TestClient(create_app(settings)) as tc:`) so the runner lifespan starts.

## Running the webapp

```
uv run jobscout serve                 # → http://127.0.0.1:8020
uv run jobscout serve --port 9000     # a different port
uv run jobscout serve --reload        # auto-reload (dev)
uv run jobscout serve --stop          # stop a server started from this project
```

### The Windows "file in use" gotcha (important)

A running `jobscout serve` **holds a lock on `.venv/Scripts/jobscout.exe`**, so
`uv sync` / `uv run …` fail with `os error 32: file in use` when the package
needs rebuilding (i.e. after you change code). Before rebuilding, stop the
server:

```
uv run jobscout serve --stop          # clean: reads the PID file, signals it
```

`serve` writes its PID to `data/.jobscout-serve.pid` on start and removes it on
exit; `--stop` uses that. (The `--reload` path has no PID file — its worker is a
child of the reloader; stop it with Ctrl+C in its own shell.) If a server was
hard-killed and left a stale port, `--stop` detects the dead PID and cleans up;
as a last resort find the listener with `Get-NetTCPConnection -LocalPort 8020`.

## Smoke checks (fast, no real server)

```
uv run python scripts/webapp_smoke.py       # drives the webapp routes in-process
uv run python scripts/routes_smoke.py       # Google Routes key + commute wiring
```

`webapp_smoke.py` runs the real app via `TestClient` against a throwaway temp DB
— it exercises the read views, a review write, and a per-offer run trigger, and
**can't leak a server process**. Use it to sanity-check webapp changes end-to-end
without launching (and having to stop) a real server.

## Branch → PR → merge flow

Work on a branch, never commit straight to `main`:

```
git checkout -b feat/thing            # feat/… fix/… chore/… docs/…
# … edit, then: uv run pytest && uv run ruff check src tests …
git add <files> && git commit         # message ends with the Co-Authored-By + Claude-Session trailers
git push -u origin feat/thing
gh pr create --base main --head feat/thing --title "…" --body "…"
gh pr merge <n> --merge
git checkout main && git pull --ff-only
git branch -d feat/thing && git push origin --delete feat/thing
```

Keep commits green (tests + ruff pass before committing). Split large work into
logically-layered commits (e.g. pipeline change → storage change → web change)
so each is reviewable on its own — see the webapp-v2 PRs for the pattern.

## Where things live

- `src/jobscout/` — the package. `pipeline/` (stage orchestration, one `run_*`
  each), `storage/db.py` (schema + writes), `storage/{queries,stats}.py`
  (webapp reads), `web/` (the FastAPI app), `adapters/`, `enrich/`, `agents/`,
  `llm/`, `cli.py`.
- `tests/` — offline, fixture-based. `_seed(...)` helpers set up temp DBs the way
  the pipeline would leave them; copy the nearest existing one.
- `scripts/` — throwaway verification/smoke tooling (not shipped in the package).
- `data/` (gitignored) — `jobs.db`, the LinkedIn guest-page cache, the serve PID.
- `docs/` — as-built docs per area (`webapp.md`, `scoring.md`, `enrichment.md`,
  `ingest.md`, `agents.md`, `architecture.md`) + the project brief.

## Conventions

- Idempotent stages: a re-run only processes "new" work; each has `--dry-run`.
- Every LLM interaction is logged in full to `logs/`.
- Fully local: no cloud LLM APIs, no CDN, `serve` binds `127.0.0.1` only, no
  email-send anywhere. See `CLAUDE.md` for the full golden rules.

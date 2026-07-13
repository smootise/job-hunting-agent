# Address & commute enrichment (as-built)

The Phase 2 stage that turns a filtered offer into an offer with a resolved
office address and a realistic commute time from the owner's home. It runs after
the hard filters and before LLM scoring, on `passed` + `needs_review` offers
only (rejected offers never cost an API call).

CLI: `jobscout enrich-commute [--limit N] [--re-enrich] [--dry-run]`.

Code: `enrich/geocode.py`, `enrich/address.py`, `enrich/routing.py` (pure-ish,
each with an injectable HTTP client) + `pipeline/enrich_commute.py`
(orchestration, mirroring `filter_stage.py` / `enrich_linkedin.py`).

## The pipeline for one offer

```
JobRecord ──▶ resolve_address ──┬─ remote?      → commute_minutes = 0, skip routing
                                ├─ needs_address? → skip routing, flag for Phase 3
                                ├─ resolved?    → plan_commute (3 strategies) → best
                                └─ unresolved?  → leave NULL, fail-soft (retry next run)
```

- **Address resolution** (`enrich/address.py`) is a deterministic chain:
  1. a **street address parsed from the posting prose** (conservative regex),
     geocoded and region-checked → `address_source='posting'`, high confidence;
  2. **bare "Paris"** (no arrondissement/postcode/street) → **too vague to
     route**: skip geocoding, `address_source='needs_address'`, commute NULL.
     Paris spans 20 arrondissements — a centroid commute is a confident-looking
     wrong number, so we flag it for the Phase 3 agent rather than store noise.
     ("Paris 11e"/"Paris 75011" carry real specificity and *are* routed; the
     guard is deliberately Paris-only, not every big city.);
  3. the **stated city** geocoded → `medium` confidence, tagged with the offer's
     source (`wttj`/`france_travail`/…);
  4. an **out-of-Île-de-France** hit is kept but flagged `approximate` / `low`
     (it can inform scoring but must never hard-reject — the brief's cardinal
     rule against a wrong address silently killing an offer);
  5. nothing usable → `unresolved` (row left for a later run).
  The online **address-research agent is deliberately Phase 3** (it's the warm-up
  for the agent-loop machinery); this stage's city fallback stands in for it, and
  the `needs_address` rows are its explicit worklist.

  `needs_address` vs. `unresolved`: `needs_address` means "we *have* a location
  but it's too vague to route" (stamped `enriched_at` → a stable worklist, not
  re-tried every run); `unresolved` means "no usable location text at all" (left
  retryable). Both carry a NULL commute.

- **Geocoding** (`enrich/geocode.py`) is **Base Adresse Nationale**
  (`api-adresse.data.gouv.fr`) — free, keyless, official, France-accurate — and
  doubles as the **region-validation gate** (department code from postcode/
  context; IDF = 75/77/78/91/92/93/94/95).

## The three commute strategies

Rather than one commute number, every routable offer gets **three**, all stored
(the owner reviews them); the fastest becomes the headline `commute_minutes` /
`commute_mode`:

| Strategy | What it is |
|---|---|
| `no_bike` | Plain Google transit, any mode (metro/bus/walk). The baseline. |
| `bike_only` | Door-to-door bike. (A fast e-scooter ≈ Google's bike time, per the owner's real-world calibration.) |
| `bike_hybrid` | Rail/RER line-haul, with slow **walk connector legs swapped for bike** where worthwhile — the owner's actual commute (scooter last-mile off a mainline station). |

`bike_hybrid` is composed in our code, not asked of the API: no routing provider
models "transit + my own scooter." We take Google's **rail-only** itinerary
(TRAIN/RAIL/LIGHT_RAIL, excluding SUBWAY/BUS), keep the line-haul, and re-time
each `WALK` connector at bike speed when it's worth it. This is an **estimate on
purpose** — we bike between a step's own endpoints rather than re-optimising
which station to use — because "a good estimate" is the stated goal.

### The two bike bounds (`preferences.yaml` → `commute:`)

A connector is biked only **within a band**:

- `min_bike_walk_minutes` (default ~12): a walk connector at/under this stays a
  walk (not worth deploying the scooter for a short hop).
- `max_bike_distance_km` (default ~10): never bike a single leg longer than this
  (take transit instead).

**Zero / null disables the guard — never a bare comparison.** This is the
CLAUDE.md null/0-disables trap applied here:

- `max_bike_distance_km: 0`/null → **biking is off entirely**: `bike_only` and
  `bike_hybrid` are skipped and **no bike API call is ever made** (not "bike
  anything ≤ 0 km"). `config.CommutePrefs.bike_enabled` gates this *before* any
  request.
- `min_bike_walk_minutes: 0`/null → **no lower gate**: any leg within the
  distance bound may bike; no spurious walk-vs-bike comparison is added.

## Provider consolidation — a deliberate deviation from the brief

The brief specified **IDFM PRIM** (free key) as the *sole* external routing
service for transit, with a *local* OSRM instance for driving, chosen
specifically to keep routing on-machine. During planning the owner opted instead
for **Google Routes API for everything** — transit, traffic-aware driving, and
bike — with one key. Rationale, recorded here so the divergence isn't silent:

- Google does all three modes from one endpoint, directly enabling the
  "fastest of the modes" model above with far less integration.
- It **drops the untested PRIM key**, which was this stage's main risk.
- Vanilla OSRM has **no traffic model** (free-flow times only), so it couldn't
  give the rush-hour driving the owner wanted anyway; a traffic-aware provider
  was going to be needed regardless.

**The cost, accepted knowingly:** both routing modes now go to a cloud service
(Google) that receives the owner's **home coordinates** — further from the
brief's on-machine ideal than "PRIM + cloud driving," though the marginal
privacy delta is nil (same single external recipient either way).

### Security invariants held (and enforced in code)

- The **home coordinates** live only in gitignored `preferences.yaml`, are
  resolved once per run, and enter **only** the Google request body. They are
  **never** written to the DB (only the *office* address/coords + commute
  *minutes* are stored), never logged (routing logs mode + error *type* only,
  never coords), never placed in a summary, and — downstream — never in an LLM
  prompt/digest/letter. Tests assert this: `test_routing.py::
  test_home_coords_never_logged` and `test_enrich_commute.py::
  test_home_coords_never_persisted`.
- All routing/geocoding is **fail-soft**: a miss/quota/error drops that strategy
  (or leaves the row NULL) and the batch continues — never a crash.

## Stored columns (on `jobs`)

`address`, `lat`, `lon` (the **office**), `address_source`
(`posting|wttj|france_travail|linkedin_email|approximate|remote|needs_address|unresolved`),
`address_confidence` (`high|medium|low`), `commute_minutes` (headline, fastest),
`commute_mode` (winning strategy), `commute_strategies` (JSON: all three +
per-leg detail, for review), `enriched_at` (idempotency stamp — a re-run skips
enriched rows; `--re-enrich` redoes all).

## What the LLM scorer consumes (handoff)

The next stage (`weekly_commute_fit`) reads `commute_minutes` and is **computed
in Python, not by the LLM** (owner's decision — a deterministic curve over
`one-way × 2 × onsite_days`, so re-scoring after a better address is free). What
a scoring run finds per `address_source`:

- `remote` → `commute_minutes = 0` → max score on the criterion.
- a routed source (`posting`/`wttj`/`france_travail`/`linkedin_email`) → a real
  number; `approximate` (confidence `low`) is a *usable but estimated* number —
  score it, flag it for review.
- `needs_address` or a NULL `commute_minutes` (routing failed / `unresolved`) →
  **no usable commute: unknown, not zero.** Flag the criterion; never max it or
  tank it. These are exactly the rows the Phase 3 address agent will sharpen,
  after which a `--rescore` recomputes the curve.

The LLM still infers `onsite_days` from the posting and scores the qualitative
criteria; `weekly_commute_fit` stays out of its JSON schema and is blended into
the weighted total afterward. See `docs/architecture.md` → "Current state & next"
for the full scoring handoff.

## Departure time

A single representative weekday-morning arrival (~09:00 Europe/Paris) for both
traffic-aware driving and timetable transit. An AM/PM split is a non-breaking
later add (we store minutes, not a fixed pair).

## Prerequisites (owner, out-of-band)

- A Google Cloud project with **billing enabled** (card required even for the
  free tier; set a budget cap / quota so it can never charge), the **Routes API**
  enabled, and an API key in `.env` as `GOOGLE_ROUTES_KEY`. Free tier easily
  covers job-hunt volume.
- A geocodable **home address** (or pinned `home.lat`/`home.lon`) in
  `preferences.yaml`.
- Validate the key first: `uv run python scripts/routes_smoke.py` routes
  home → La Défense per mode and prints minutes — a green run means the provider
  wiring works before you run the real stage.

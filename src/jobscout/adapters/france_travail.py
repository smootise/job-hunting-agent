"""France Travail (Offres d'emploi v2) adapter.

France Travail is the official French public employment service. Its job API
is free, documented, and — unlike WTTJ — requires real OAuth2 credentials
(register an app at francetravail.io and subscribe it to the "Offres d'emploi
v2" API). We authenticate with the `client_credentials` grant (no user
involved; this is a server-to-server app token), then call the search
endpoint with a Bearer token.

Confirmed live during Phase 1 build:
  - Token URL: entreprise.francetravail.fr/.../access_token, `realm=/partenaire`.
  - Scope: "api_offresdemploiv2 o2dsoffre".
  - Search returns HTTP 206 (Partial Content) for ranged results — expected,
    not an error; results are in `resultats[]`.

Filtering philosophy (Phase 1 plan): filter only on keywords (`motsCles`) and
region (`region=11`, the France Travail code for Île-de-France — one param
covering all 8 departments). We deliberately send NO `typeContrat` filter —
France Travail returns CDD/interim/etc. too, and Phase 2's transparent hard
filter keeps CDI while flagging unstated ones for review. An API-side CDI
filter would silently drop CDI-implied postings.

Pagination note (confirmed live): results are paged via a `range=start-end`
param whose span must be **≤ 150**; exceeding it returns 400 "plage trop
importante". The `Content-Range: offres 0-N/TOTAL` response header gives the
total, which we use as the stop condition.
"""

from __future__ import annotations

import httpx

from jobscout import normalize
from jobscout.config import FranceTravailCredentials, france_travail_credentials
from jobscout.models import JobRecord

_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
_SCOPE = "api_offresdemploiv2 o2dsoffre"

# France Travail's region code for Île-de-France. One param covers all 8
# departments (75,77,78,91,92,93,94,95).
_IDF_REGION = "11"

# Broad PM/PO net (same rationale as the WTTJ adapter).
_QUERIES = ("product manager", "product owner")

# France Travail rejects `range` spans wider than 150 (400 error). Stay under.
_PAGE_SIZE = 100


def fetch(
    *,
    limit: int = 200,
    credentials: FranceTravailCredentials | None = None,
    client: httpx.Client | None = None,
) -> list[JobRecord]:
    """Fetch up to `limit` PM/PO offers across Île-de-France from France Travail.

    Obtains a fresh app token, runs each broad query paginated up to the cap,
    and maps results to JobRecords. Credentials/client are injectable for
    testing; in production both are created here with timeouts.
    """
    credentials = credentials or france_travail_credentials()
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        token = _get_token(client, credentials)
        auth = {"Authorization": f"Bearer {token}"}
        records: list[JobRecord] = []
        for query in _QUERIES:
            records.extend(_fetch_query(client, auth, query, limit))
            if len(records) >= limit:
                break
        return records[:limit]
    finally:
        if owns_client:
            client.close()


def _get_token(
    client: httpx.Client, credentials: FranceTravailCredentials
) -> str:
    """Exchange client credentials for a short-lived Bearer token."""
    response = client.post(
        _TOKEN_URL,
        params={"realm": "/partenaire"},
        data={
            "grant_type": "client_credentials",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scope": _SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _fetch_query(
    client: httpx.Client, auth: dict[str, str], query: str, limit: int
) -> list[JobRecord]:
    """Paginate one query via the `range=start-end` param until the cap.

    Stops when we've hit `limit`, drained the result set, or reached the
    total advertised by the `Content-Range` header. Spans stay ≤ _PAGE_SIZE
    (<150) to avoid France Travail's "plage trop importante" 400.
    """
    out: list[JobRecord] = []
    start = 0
    while len(out) < limit:
        end = start + _PAGE_SIZE - 1
        response = client.get(
            _SEARCH_URL,
            headers=auth,
            params={
                "motsCles": query,
                "region": _IDF_REGION,
                "range": f"{start}-{end}",
            },
        )
        # 200 = full set fit; 206 = partial (ranged) — both are success.
        # 204 = no content (empty result set) — stop cleanly.
        if response.status_code == 204:
            break
        response.raise_for_status()
        results = response.json().get("resultats", [])
        if not results:
            break
        out.extend(parse_offer(o) for o in results)
        total = _total_from_content_range(response.headers.get("Content-Range"))
        start += _PAGE_SIZE
        if total is not None and start >= total:
            break  # fetched everything the query has
    return out


def _total_from_content_range(header: str | None) -> int | None:
    """Parse the total count out of a 'offres 0-49/72' Content-Range header."""
    if not header or "/" not in header:
        return None
    try:
        return int(header.rsplit("/", 1)[1])
    except ValueError:
        return None


def parse_offer(offer: dict) -> JobRecord:
    """Map one France Travail offer to a JobRecord.

    Pure/side-effect-free for fixture testing. Notable mappings:
      - url: the public candidate-facing detail page (`origineOffre.urlOrigine`).
      - contract: `typeContrat` is a code ('CDI'/'CDD'/'MIS'); we normalize it.
      - salary: `salaire.libelle` when present, else the free-text
        `commentaire` ('Selon profil', 'N/A'); None when neither is useful.
      - lang: France Travail postings are French; detect from text to be safe.
    """
    entreprise = offer.get("entreprise") or {}
    lieu = offer.get("lieuTravail") or {}
    origine = offer.get("origineOffre") or {}
    description = offer.get("description")

    return JobRecord(
        source="france_travail",
        external_id=str(offer.get("id") or ""),
        url=origine.get("urlOrigine") or "",
        title=offer.get("intitule") or "",
        company=entreprise.get("nom") or "",
        location=lieu.get("libelle"),
        contract_type=normalize.parse_contract_type(offer.get("typeContrat")),
        salary_text=_format_salary(offer.get("salaire")),
        description=description,
        posted_at=offer.get("dateCreation"),
        lang=normalize.detect_language(offer.get("intitule"), description),
    )


def _format_salary(salaire: dict | None) -> str | None:
    """Prefer a real salary label; fall back to the comment; else None.

    France Travail often has no structured salary, only a free-text comment
    like 'Selon profil' or 'N/A' — those carry no number, so we return them
    as-is for the human digest but they won't trip the Phase 2 salary floor
    (which only applies to offers that STATE a number)."""
    if not salaire:
        return None
    label = salaire.get("libelle")
    if label:
        return label
    comment = salaire.get("commentaire")
    if comment and comment.strip().upper() not in {"N/A", "NA"}:
        return comment
    return None

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

import re

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


# ----------------------------------------------------------------------------
# Company-name recovery for ANONYMOUS postings
# ----------------------------------------------------------------------------
#
# Many France Travail employers post without naming themselves: `entreprise.nom`
# is legitimately absent (not an adapter bug). That leaves `company=''`, which
# would send the Phase 3 company-research agent searching on an empty string. So
# we make a *conservative, high-precision* attempt to recover the name from the
# offer's own text — recovering a few reliably beats guessing (a wrong company
# name pollutes the brief and the score). When nothing clears the bar we leave
# `company=''` and the downstream agent skips research for that row.
#
# Precedence: the FR description opener ("<Company> est un/une…") wins over a
# title pattern, because the data shows they can disagree — e.g. a title reading
# "… lgi f/h" whose description opens "Safran est un groupe…" (Safran is the real
# employer; "lgi" is a brand/subsidiary in the title).

# "COMPANY est un/une/le/la/l' …" — the classic FR company self-intro opener.
# Capture 1-4 leading tokens (proper-noun-ish) before " est ".
_DESC_INTRO_RE = re.compile(
    r"^\s*([A-ZÀ-Ý][\w&.\-]*(?:\s+[A-ZÀ-Ý0-9][\w&.\-]*){0,3})\s+est\s+(?:un|une|le|la|l['’]|né|née)\b",
)

# Title "[Name] : role" — a leading bracketed company then a colon.
_TITLE_BRACKET_RE = re.compile(r"^\s*\[([^\]]{2,40})\]\s*:")

# Title "Name - role…" — a leading segment before " - ". Only trusted when the
# trailing part clearly names the role (so "Manager - Product & …" doesn't count).
_TITLE_DASH_RE = re.compile(r"^\s*([A-ZÀ-Ý][\w&.\-]{1,39}(?:\s+[\w&.\-]+){0,2})\s+-\s+(.+)$")

# Words that are NOT a company: roles, work-arrangement, and the "[Offre interne]"
# marker. If an extracted candidate is (or starts with) one of these, reject it.
_NOT_A_COMPANY = frozenset({
    "offre", "interne", "product", "senior", "lead", "data", "digital", "technical",
    "manager", "management", "responsable", "consultant", "owner", "marketing",
    "chef", "head", "principal", "staff", "poste", "cdi", "cdd", "h", "f", "hf",
})


def _looks_like_company(candidate: str) -> bool:
    """Reject role words / markers / obvious non-companies (precision over recall).

    A candidate passes only if it's a plausible org name: 2-40 chars, not a known
    role/marker token, not purely a generic word. This is the gate that keeps
    recovery from inventing a wrong company (which would be worse than none).
    """
    c = candidate.strip().strip(".-–—:").strip()
    if not (2 <= len(c) <= 40):
        return False
    # Reject if ANY token is a role/marker word — this is what stops
    # "Offre interne", "Product Manager", "Senior …" etc. from being taken as a
    # company. (Checking every token, not just the first, catches "Lead Data …".)
    tokens = [t for t in re.split(r"[\s\-/]+", c) if t]
    if any(t.lower() in _NOT_A_COMPANY for t in tokens):
        return False
    # Must contain at least one letter. Case is NOT required to be uppercase —
    # real brands are often stylized lowercase ("s3ns", "doctolib"); the
    # structural signal (a bracket, or "X est un…") is what gives us confidence.
    return any(ch.isalpha() for ch in c)


def recover_company(title: str | None, description: str | None) -> str | None:
    """Best-effort company name from an anonymous offer's text, or None.

    High-precision: tries the FR description opener first, then the "[Name] :" and
    "Name - role" title patterns, validating each candidate with
    ``_looks_like_company``. Returns None when nothing clears the bar (caller keeps
    ``company=''``). Never raises."""
    desc = (description or "").strip()
    if desc:
        m = _DESC_INTRO_RE.match(desc)
        if m and _looks_like_company(m.group(1)):
            return m.group(1).strip()

    t = (title or "").strip()
    if t:
        m = _TITLE_BRACKET_RE.match(t)
        if m and _looks_like_company(m.group(1)):
            return m.group(1).strip()
        m = _TITLE_DASH_RE.match(t)
        if m and _looks_like_company(m.group(1)):
            return m.group(1).strip()
    return None


def parse_offer(offer: dict) -> JobRecord:
    """Map one France Travail offer to a JobRecord.

    Pure/side-effect-free for fixture testing. Notable mappings:
      - url: the public candidate-facing detail page (`origineOffre.urlOrigine`).
      - contract: `typeContrat` is a code ('CDI'/'CDD'/'MIS'); we normalize it.
      - salary: `salaire.libelle` when present, else the free-text
        `commentaire` ('Selon profil', 'N/A'); None when neither is useful.
      - company: `entreprise.nom` when the employer named itself; else a
        conservative best-effort recovery from the offer text (anonymous
        postings — see ``recover_company``), else '' (downstream skips research).
      - lang: France Travail postings are French; detect from text to be safe.
    """
    entreprise = offer.get("entreprise") or {}
    lieu = offer.get("lieuTravail") or {}
    origine = offer.get("origineOffre") or {}
    description = offer.get("description")
    title = offer.get("intitule") or ""

    company = entreprise.get("nom") or recover_company(title, description) or ""

    return JobRecord(
        source="france_travail",
        external_id=str(offer.get("id") or ""),
        url=origine.get("urlOrigine") or "",
        title=title,
        company=company,
        location=lieu.get("libelle"),
        contract_type=normalize.parse_contract_type(offer.get("typeContrat")),
        salary_text=_format_salary(offer.get("salaire")),
        description=description,
        posted_at=offer.get("dateCreation"),
        lang=normalize.detect_language(title, description),
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

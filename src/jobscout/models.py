"""The common job-record shape shared by every source adapter.

Why one shape for all sources: Welcome to the Jungle, France Travail, and
LinkedIn alert emails each return wildly different native payloads (Algolia
JSON, France Travail's REST schema, HTML email). If each source's quirks
leaked downstream, every later stage (dedupe, hard filters, enrichment,
scoring) would need per-source special cases. Instead each adapter is
responsible for one job: turn its native payload into a `JobRecord`. From
that point on, the whole pipeline is *source-agnostic* — it only ever sees
`JobRecord`s.

`JobRecord` is a plain frozen dataclass, not Pydantic: this is a learning
project that favors "explicit over clever, no framework" (see CLAUDE.md and
the note in config.py about deferring typed models). A dataclass is enough —
it documents the exact fields, is immutable so a record can't be mutated
mid-pipeline by accident, and needs no third-party dependency.

The record deliberately holds only what a *source* can know. Everything the
pipeline computes later — commute minutes, resolved address, LLM scores,
dedupe grouping — lives as extra columns on the SQLite `jobs` table
(see storage/db.py), not on this object. Keeping `JobRecord` to the source
contract is what makes it a stable interface between adapters and storage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

# The canonical field order / names. Every adapter emits exactly these, and
# the SQLite `jobs` table stores exactly these (plus its own bookkeeping
# columns). Keeping the list in one place means a schema change is a one-line
# edit here that the row round-trip below picks up automatically.
FIELD_NAMES: tuple[str, ...] = (
    "source",
    "external_id",
    "url",
    "title",
    "company",
    "location",
    "contract_type",
    "salary_text",
    "description",
    "posted_at",
    "lang",
)


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One job offer as a single source reported it.

    Fields that a source does not provide are ``None`` — never omitted. A
    missing field is information ("this source didn't say"), and later stages
    rely on it: e.g. an unstated ``contract_type`` becomes ``needs_review``
    rather than a silent rejection (CLAUDE.md's cardinal rule).
    """

    source: str
    """Which adapter produced this: 'wttj' | 'france_travail' | 'linkedin_email'."""

    external_id: str
    """Source-native stable id. With `source`, this is the primary dedupe key
    — re-seeing the same (source, external_id) must never create a second row."""

    url: str
    """Public link to the posting, for the digest and the cover-letter agent's
    domain whitelist."""

    title: str
    company: str

    location: str | None
    """Free-text location as the source stated it (e.g. 'Paris, France',
    'Remote'). Address resolution + commute are a Phase 2 concern."""

    contract_type: str | None
    """Normalized contract type ('CDI', 'CDD', ...) or None when unstated.
    None must never hard-reject — it flags needs_review in Phase 2."""

    salary_text: str | None
    """Raw salary string as stated, or None. Parsing the range's upper bound
    for the salary floor is a Phase 2 concern; we keep the raw text here."""

    description: str | None
    posted_at: str | None
    """ISO-8601 timestamp string, or None when the source omits it (common in
    LinkedIn alert emails)."""

    lang: str | None
    """'fr' | 'en', detected during normalization. Drives which master cover
    letter the Phase 3 letter agent adapts. None only if detection is skipped."""

    def to_row(self) -> dict[str, Any]:
        """Return this record as a dict keyed by column name, for SQLite.

        Just the source fields — the storage layer adds its own bookkeeping
        columns (first_seen_at, normalized_company, dup_group, ...) around
        these. Using a dict (not a positional tuple) keeps inserts robust to
        column reordering.
        """
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> JobRecord:
        """Rebuild a JobRecord from a DB row (or any mapping).

        Ignores extra keys the row may carry (the bookkeeping columns), so a
        `SELECT *` can be handed straight in without projecting first.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: row[k] for k in known})

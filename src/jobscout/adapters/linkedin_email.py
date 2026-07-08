"""LinkedIn adapter — parses job-alert emails over read-only IMAP.

This is the one source with no API. LinkedIn sends the owner *job-alert
emails* (from a saved search); we read them from a dedicated, read-only IMAP
folder and parse the jobs out of the HTML. No login automation, no scraping of
LinkedIn itself — only the emails LinkedIn chose to send. The folder is opened
`readonly=True` and we never flag, move, or delete a message (CLAUDE.md's
read-only IMAP invariant).

Two email shapes must both parse (confirmed against the real mailbox):

  1. **Native** LinkedIn alert: `From: LinkedIn Job Alerts
     <jobalerts-noreply@linkedin.com>`, an HTML digest listing several jobs.
  2. **Forwarded** alert: the owner seeded the mailbox by forwarding alerts
     from another account, so the outer message is a `Fw:` from the owner with
     the original LinkedIn HTML nested inside. Structurally the forwarded HTML
     still contains the same LinkedIn job blocks, so once we reach the HTML
     part the parsing is identical — we don't need to "unwrap" anything beyond
     picking the HTML MIME part, which `email.walk()` already surfaces.

Parsing strategy (robust to LinkedIn's table-based email markup): find every
anchor to `/jobs/view/{id}` — that yields the stable job id (our
`external_id`) and the canonical URL. Then climb to the enclosing table row,
whose visible text reliably reads as:

    line 1: <job title>
    line 2: <company> · <location> (<remote policy>)

A single job appears in several anchors (a thumbnail image link + a text
link), so we dedupe by job id within the email. Everything here is pure once
the raw bytes are in hand, so `parse_alert_email` is unit-tested against a
saved `.eml` with no IMAP connection.
"""

from __future__ import annotations

import email
import imaplib
import re
from email import policy
from email.message import EmailMessage

from bs4 import BeautifulSoup

from jobscout import normalize
from jobscout.config import ImapCredentials, imap_credentials
from jobscout.models import JobRecord

DEFAULT_FOLDER = "linkedin-alerts"

# LinkedIn job-view links carry the numeric job id we use as external_id.
_JOB_VIEW_RE = re.compile(r"/jobs/view/(\d+)")

# "Company · Location (policy)" — the middle dot separates company from place.
_COMPANY_LOCATION_RE = re.compile(r"^(?P<company>.+?)\s*[·•]\s*(?P<location>.+)$")


def fetch(
    *,
    limit: int = 200,
    credentials: ImapCredentials | None = None,
    folder: str = DEFAULT_FOLDER,
) -> list[JobRecord]:
    """Fetch and parse LinkedIn job-alert emails from the dedicated folder.

    Connects read-only, reads every message in `folder`, parses each into
    zero-or-more JobRecords, and returns up to `limit` of them. IMAP details
    are isolated here; the actual parsing (`parse_alert_email`) is pure and
    testable. Credentials are injectable for tests.
    """
    credentials = credentials or imap_credentials()
    raw_messages = _fetch_raw_messages(credentials, folder, limit)
    records: list[JobRecord] = []
    for raw in raw_messages:
        records.extend(parse_alert_email(raw))
        if len(records) >= limit:
            break
    return records[:limit]


def _fetch_raw_messages(
    credentials: ImapCredentials, folder: str, limit: int
) -> list[bytes]:
    """Return raw RFC822 bytes for messages in `folder`, newest first.

    Read-only throughout: `select(readonly=True)` opens the folder in EXAMINE
    mode, so fetching does not even set the \\Seen flag. We never issue a
    store/copy/expunge.
    """
    conn = imaplib.IMAP4_SSL(credentials.host)
    try:
        conn.login(credentials.user, credentials.app_password)
        conn.select(folder, readonly=True)
        _typ, data = conn.search(None, "ALL")
        ids = data[0].split()
        # Newest first, capped: alert emails each hold several jobs, so we
        # don't need many to hit `limit`.
        ids = list(reversed(ids))[: max(limit, 1)]
        raw_messages: list[bytes] = []
        for msg_id in ids:
            _typ, msg_data = conn.fetch(msg_id, "(RFC822)")
            if msg_data and msg_data[0]:
                raw_messages.append(msg_data[0][1])
        return raw_messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def parse_alert_email(raw: bytes) -> list[JobRecord]:
    """Parse one alert email's raw bytes into JobRecords (pure).

    Picks the HTML MIME part (works for both native and forwarded alerts,
    since the forwarded copy still nests LinkedIn's HTML), then extracts each
    unique job. Returns [] for a message with no HTML or no job links rather
    than raising — a stray non-alert email in the folder must not crash a run.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)
    html = _extract_html(msg)
    if not html:
        return []
    return _parse_jobs_from_html(html)


def _extract_html(msg: EmailMessage) -> str | None:
    """Return the first text/html part's decoded content, or None."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    return None


def _parse_jobs_from_html(html: str) -> list[JobRecord]:
    """Extract deduped jobs from one alert's HTML body."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[JobRecord] = []
    seen_ids: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        match = _JOB_VIEW_RE.search(anchor["href"])
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen_ids:
            continue

        title, company, location = _read_job_block(anchor)
        if not title:
            # Some anchors (thumbnail images) carry no readable block; skip
            # and let the job's text anchor supply the fields.
            continue
        seen_ids.add(job_id)

        records.append(
            JobRecord(
                source="linkedin_email",
                external_id=job_id,
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                title=title,
                company=company or "",
                location=location,
                # LinkedIn alerts don't state contract or salary; leave None
                # so Phase 2 marks them needs_review rather than dropping.
                contract_type=None,
                salary_text=None,
                description=None,
                posted_at=None,  # Alerts carry no reliable per-job post date.
                lang=normalize.detect_language(title, None),
            )
        )
    return records


def _read_job_block(anchor) -> tuple[str, str | None, str | None]:
    """From a job anchor, read (title, company, location) off its table row.

    LinkedIn lays each job out as a table row whose visible text is:
        <title> / <company> · <location (policy)> / <noise...>
    We climb to the nearest ancestor that yields at least two non-empty text
    lines and read the first two. Returns ("", None, None) if nothing usable
    is found, so the caller can skip image-only anchors.
    """
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        lines = [
            line.strip()
            for line in node.get_text("\n", strip=True).split("\n")
            if line.strip()
        ]
        if len(lines) >= 2:
            title = lines[0]
            company, location = _split_company_location(lines[1])
            return title, company, location
    return "", None, None


def _split_company_location(line: str) -> tuple[str | None, str | None]:
    """Split a 'Company · Paris (Hybrid)' line into (company, location)."""
    match = _COMPANY_LOCATION_RE.match(line)
    if match:
        return match.group("company").strip(), match.group("location").strip()
    # No separator: treat the whole line as the company.
    return line.strip() or None, None

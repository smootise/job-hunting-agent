"""FastAPI dependencies: per-request DB connection + access to app state.

The DB connection is opened per request and closed in ``finally``. SQLite is in
WAL mode (see ``db.connect``), so concurrent readers are fine, but a single
``sqlite3.Connection`` is not safe to share across threads — and FastAPI runs
sync routes in a threadpool. A fresh connection per request sidesteps that with
no locking; for a single-user local dashboard the open cost is negligible.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Request

from jobscout.storage import db


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Yield a read connection to the configured DB, closed after the request.

    Reuses ``db.connect`` (Row factory + WAL + schema ensure). The schema-ensure
    is harmless on an existing DB and means a brand-new checkout can serve pages
    against an empty-but-valid database rather than erroring.
    """
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()

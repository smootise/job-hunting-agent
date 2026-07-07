"""SQLite storage: the single source of truth for job records.

Primary key is (source, external_id); a cross-source fuzzy match on
normalized (company, title) merges the same job seen on two sources into
one record. "New since last run" is defined as: not already in
data/jobs.db.
"""

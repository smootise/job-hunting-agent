"""The read-only webapp: a FastAPI + HTMX view over ``data/jobs.db``.

V1 is deliberately read-only — it browses what the pipeline produced (dashboard,
ranked offer list, offer detail) and triggers nothing. No POST routes, no CLI
runs, no external calls; the golden rule "the agent never acts externally" holds
trivially because there are no write or action paths here at all.

The app is mounted on the existing pure read layer (``storage.queries`` +
``storage.stats``); ``create_app`` is a factory so tests can point it at a
fixture DB. See ``settings.py`` for how it locates ``data/jobs.db`` and the
gitignored ``preferences.yaml`` regardless of the launch directory.
"""

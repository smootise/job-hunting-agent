"""Job Scout: a fully-local AI agent pipeline for job hunting.

See CLAUDE.md at the repo root for the rules of engagement, and
docs/job-scout-project-brief.md for the full design rationale. This
package is deliberately plain: a deterministic pipeline (ingest, filter,
enrich, score) plus two small hand-rolled agents (address research,
cover-letter drafting) — no agent framework. The structure below mirrors
the pipeline stages one-to-one so the code doubles as a map of the
architecture.
"""

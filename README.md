# Job Scout

A fully-local AI agent pipeline that collects Product Manager / Product
Owner offers in Île-de-France, scores them against your preferences, and
drafts tailored cover letters for the best ones.

See `CLAUDE.md` for the rules of engagement and `docs/job-scout-project-brief.md`
for the full design rationale.

## Setup

```
uv sync
cp preferences.example.yaml preferences.yaml   # then edit with your own values
cp .env.example .env                            # then fill in real credentials
```

Drop your master cover letters and CV into `profile/` (see `profile/README.md`).

## Model bake-off

Before running the pipeline for real, see `scripts/bakeoff/README.md` to
compare local models on JSON reliability and letter-drafting quality.

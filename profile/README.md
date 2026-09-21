# profile/

This directory is gitignored — everything in it except this README is
personal material that never gets committed:

- `master_letter_fr.md` — your master cover letter in French. The
  cover-letter agent adapts this, never writes from scratch.
- `master_letter_en.md` — your master cover letter in English. If this
  is missing when an English draft is needed, the agent adapts from the
  FR master instead and flags the draft `⚠ translated from FR master`
  for closer review.
- `cv.md` — your CV, as Markdown or extracted plain text.

## Seeding the masters from your sent letters

`samples/` holds two real postings — NEXTON (FR) and Dataiku (EN) —
which are tracked, since they're public job ads rather than personal
material. The **sent letters** that answered them are gitignored for the
same reason everything in this directory is: they're the owner's real
career history.

If you have letters you've sent, a reasonable starting point for
`master_letter_fr.md` / `master_letter_en.md` is to take one and strip
out the posting-specific sentences, leaving the generic background, the
recurring project hook, and the closing. Otherwise just write the
masters directly — the pipeline only ever adapts them, never writes a
letter from scratch. See `scripts/bakeoff/README.md` for how the masters
get exercised in the model bake-off.

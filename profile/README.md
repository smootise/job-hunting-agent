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

`samples/` (tracked, not personal — see its own contents) has two real
posting + sent-letter pairs: NEXTON (FR) and Dataiku (EN). Both letters
already read like a master letter adapted to one posting. A reasonable
starting point for `master_letter_fr.md` / `master_letter_en.md` is to
take the matching sample letter and strip out the posting-specific
sentences, leaving the generic background, the recurring project hook,
and the closing — see `scripts/bakeoff/README.md` for how these masters
get exercised in the model bake-off.

# Vendored static assets

These are committed to the repo (no CDN) per the project's "fully local" golden
rule — the webapp must not fetch anything from an external host at runtime.

- **`htmx.min.js`** — htmx **2.0.4**, from `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`.
  To upgrade: re-download the pinned version, update this note, and re-test the
  offer-list sort/filter swaps.
- **`app.css`** — hand-written, theme-aware (light/dark), no framework.

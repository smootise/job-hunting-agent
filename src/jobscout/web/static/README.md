# Vendored static assets

These are committed to the repo (no CDN) per the project's "fully local" golden
rule — the webapp must not fetch anything from an external host at runtime.

- **`htmx.min.js`** — htmx **2.0.4**, from `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`.
  To upgrade: re-download the pinned version, update this note, and re-test the
  offer-list sort/filter swaps.
- **`app.css`** — hand-written, theme-aware (light/dark), no framework.
- **`eudate.js`** — hand-written, no dependencies. Renders the offer-list date
  filters as `dd/mm/yyyy` on every browser. A native `<input type="date">` can't
  be told what format to display (the DOM value is always ISO, and the rendering
  follows the browser/OS locale — Chrome reads the document's `lang`, but Firefox
  and Safari ignore it), so each filter date is a visible EU-formatted text input
  paired with a hidden date input that carries the ISO value and submits. See the
  header comment in the file and `offers/_datefield.html`.

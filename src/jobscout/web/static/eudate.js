/*
 * EU-format (dd/mm/yyyy) date fields for the offer-list filters.
 *
 * WHY THIS EXISTS
 * A native `<input type="date">` gives the page no control over its displayed
 * format: the DOM value is always ISO `yyyy-mm-dd`, and the visible rendering
 * comes from the browser/OS locale. Chrome follows the document's `lang`, so
 * `lang="en-GB"` is enough there — but Firefox and Safari ignore `lang` and use
 * the OS regional settings, which for a US-English install means `mm/dd/yyyy`
 * no matter what the page says. Nothing in CSS or HTML overrides it.
 *
 * HOW IT WORKS
 * Each filter date is a PAIR of fields:
 *   - a visible `type="text"` the user reads and types in dd/mm/yyyy, and
 *   - a hidden `type="date"` that carries the ISO value and is the one actually
 *     named/submitted (so the server contract is unchanged — the route still
 *     receives and validates `yyyy-mm-dd`).
 * The small button opens the hidden input's native calendar via showPicker(),
 * so date-picking by click survives.
 *
 * PROGRESSIVE ENHANCEMENT
 * The markup works without this script: the hidden date input holds the real
 * value, and `data-eudate` wiring only runs if JS does. If the script fails to
 * load, the pair is revealed as a plain (locale-formatted) date input rather
 * than leaving the user with a dead text box — see `revealFallback`.
 */
(function () {
  "use strict";

  /** ISO `yyyy-mm-dd` -> `dd/mm/yyyy` ('' for empty/invalid). */
  function isoToEu(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? `${m[3]}/${m[2]}/${m[1]}` : "";
  }

  /**
   * `dd/mm/yyyy` -> ISO `yyyy-mm-dd`, or null if unparseable.
   *
   * Accepts `/`, `-` or `.` separators and 1-2 digit day/month, so "4/10/2026"
   * and "04.10.2026" both work. Validates the date really exists (round-trips
   * through Date), so 31/02 is rejected rather than silently rolling over to
   * March — a wrong-but-accepted date would quietly filter out real offers.
   */
  function euToIso(text) {
    const m = /^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$/.exec((text || "").trim());
    if (!m) return null;
    const day = Number(m[1]);
    const month = Number(m[2]);
    const year = Number(m[3]);
    const dt = new Date(Date.UTC(year, month - 1, day));
    if (
      dt.getUTCFullYear() !== year ||
      dt.getUTCMonth() !== month - 1 ||
      dt.getUTCDate() !== day
    ) {
      return null; // e.g. 31/02/2026
    }
    const pad = (n) => String(n).padStart(2, "0");
    return `${year}-${pad(month)}-${pad(day)}`;
  }

  /** If we can't enhance, show the native input so the control still works. */
  function revealFallback(wrap) {
    const hidden = wrap.querySelector('input[type="date"]');
    if (hidden) hidden.classList.remove("eud-hidden");
    const text = wrap.querySelector('input[type="text"]');
    if (text) text.remove();
    const btn = wrap.querySelector("button");
    if (btn) btn.remove();
  }

  function wire(wrap) {
    const iso = wrap.querySelector('input[type="date"]');
    const text = wrap.querySelector('input[data-eudate-text]');
    const button = wrap.querySelector('[data-eudate-open]');
    if (!iso || !text) {
      revealFallback(wrap);
      return;
    }

    text.value = isoToEu(iso.value);

    // Typing in the text field updates the ISO field, which is what submits.
    // We dispatch `change` on it so htmx's form trigger fires exactly as it
    // would for a native date input.
    function commit() {
      const raw = text.value.trim();
      if (raw === "") {
        if (iso.value !== "") {
          iso.value = "";
          iso.dispatchEvent(new Event("change", { bubbles: true }));
        }
        text.classList.remove("eud-invalid");
        return;
      }
      const parsed = euToIso(raw);
      if (parsed === null) {
        // Flag it and DON'T submit: a half-typed or impossible date must not
        // silently become a filter that hides offers.
        text.classList.add("eud-invalid");
        return;
      }
      text.classList.remove("eud-invalid");
      if (iso.value !== parsed) {
        iso.value = parsed;
        iso.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    // `change` (not every keystroke) so a partially-typed date isn't parsed.
    text.addEventListener("change", commit);
    text.addEventListener("blur", commit);
    text.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
      }
    });

    // Picking from the native calendar writes ISO; mirror it back as EU text.
    iso.addEventListener("change", function () {
      text.value = isoToEu(iso.value);
      text.classList.remove("eud-invalid");
    });

    if (button) {
      button.addEventListener("click", function () {
        // showPicker() is the only way to open a native calendar programmatically.
        // Unsupported (or blocked outside a user gesture) -> focus the input,
        // which at least opens the picker in some browsers.
        if (typeof iso.showPicker === "function") {
          try {
            iso.showPicker();
            return;
          } catch (err) {
            /* fall through to focus */
          }
        }
        iso.classList.remove("eud-hidden");
        iso.focus();
      });
    }
  }

  function init(root) {
    (root || document).querySelectorAll("[data-eudate]").forEach(wire);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
  });
  // htmx swaps only the table, not the filter form, so re-wiring after a swap
  // isn't needed today — but this keeps the enhancement correct if a future
  // partial ever replaces a date field.
  document.addEventListener("htmx:afterSwap", function (event) {
    init(event.target);
  });
})();

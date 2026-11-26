// The "Button for your website" tab of the owner's Page design screen: the owner picks the button's text (one of our
// suggestions or their own), sees it, and copies one piece of code to paste into their own website, or emails it to
// whoever looks after that website. All text reaches the page through textContent. The code is built by
// look-model.js, which tidies and escapes the text and checks the colours.

import { el, fill } from "./gn.js";
import { contrast } from "./look.js";
import {
  BUTTON_TEXT_MAX,
  BUTTON_TEXTS,
  buttonSnippet,
  buttonStyle,
  buttonTextProblem,
  copiedCodeIsStale,
  developerEmail,
  normaliseButtonText,
  readSavedButtonText,
  snippetFingerprint,
} from "./look-model.js";
import { bookingLink } from "./owner.js";

const input = (id) => document.getElementById(id);

/** The value of the drop-down's last choice, which lets the owner type their own text. */
const OWN_TEXT = "__own__";

/**
 * The browser-storage key that remembers which code the owner last copied.
 * @param {string} slug The business slug.
 * @returns {string} The key.
 */
const storageKey = (slug) => `gn_button_${slug}`;

/**
 * The browser-storage key that remembers the button text the owner last used.
 * @param {string} slug The business slug.
 * @returns {string} The key.
 */
const textKey = (slug) => `gn_button_text_${slug}`;

/**
 * Read the fingerprint of the code the owner last copied.
 * @param {string} slug The business slug.
 * @returns {string|null} The fingerprint, or null if there is none or storage is unavailable.
 */
function lastCopied(slug) {
  try {
    return localStorage.getItem(storageKey(slug));
  } catch {
    return null;
  }
}

/**
 * Remember the fingerprint of the code the owner just copied.
 * @param {string} slug The business slug.
 * @param {string} fingerprint The fingerprint.
 */
function rememberCopied(slug, fingerprint) {
  try {
    localStorage.setItem(storageKey(slug), fingerprint);
  } catch {
    // Storage unavailable: the owner just won't be reminded to copy again.
  }
}

/**
 * Read the button text the owner last used, checked again because it was stored in the browser.
 * @param {string} slug The business slug.
 * @returns {string|null} The text, or null if there is none, it is not usable, or storage is unavailable.
 */
function lastText(slug) {
  try {
    return readSavedButtonText(localStorage.getItem(textKey(slug)));
  } catch {
    return null;
  }
}

/**
 * Remember the button text the owner is using, so a reload does not put the first suggestion back.
 * @param {string} slug The business slug.
 * @param {string} text A text that has passed the checks.
 */
function rememberText(slug, text) {
  try {
    localStorage.setItem(textKey(slug), text);
  } catch {
    // Storage unavailable: the text goes back to the first suggestion after a reload.
  }
}

/**
 * Show a button in the colours and shape of a look, as the owner's website will show it.
 * @param {HTMLElement} holder Where to put it.
 * @param {string} text The button text.
 * @param {object} look The look.
 */
function drawButtonPreview(holder, text, look) {
  const { background, color, borderRadius } = buttonStyle(look, contrast);
  const button = el("span", { "aria-hidden": "true" }, text);
  Object.assign(button.style, {
    display: "inline-block",
    padding: "12px 22px",
    borderRadius,
    background,
    color,
    fontWeight: "600",
  });
  fill(holder, button);
}

/**
 * Wire the tab up. Call `draw` whenever the look customers see changes.
 * @param {string} slug The business slug.
 * @returns {{draw: (live: {look: object, allowed: boolean}) => void}} Redraws the tab for the look customers see now,
 *   and whether the plan includes branding.
 */
export function setUpButtonTab(slug) {
  const saved = lastText(slug);
  let text = saved || BUTTON_TEXTS[0];
  let own = Boolean(saved) && !BUTTON_TEXTS.includes(saved);
  let code = "";
  let live = null;

  fill(
    input("btn-text"),
    ...BUTTON_TEXTS.map((t) => el("option", { value: t }, t)),
    el("option", { value: OWN_TEXT }, "Write my own…"),
  );
  input("btn-text").value = own ? OWN_TEXT : text;
  input("btn-custom").value = own ? text : "";
  input("btn-custom-row").hidden = !own;

  /**
   * Turn the copy button and the email link on or off together, since both need valid code.
   * @param {boolean} on Whether there is code to copy.
   */
  function allow(on) {
    input("btn-copy").disabled = !on;
    const mail = input("btn-mail");
    mail.setAttribute("aria-disabled", String(!on));
    mail.tabIndex = on ? 0 : -1;
    if (!on) mail.setAttribute("href", "#");
  }

  /** Rebuild the code, the previews, the email link and the "copy again" note. */
  function draw(next) {
    if (next) live = next;
    if (!live) return;
    const typed = own ? input("btn-custom").value : text;
    const problem = buttonTextProblem(typed);
    const count = [...normaliseButtonText(typed)].length;
    input("btn-count").textContent = own ? `${count} of ${BUTTON_TEXT_MAX}` : "";
    const note = input("btn-text-error");
    note.textContent = own ? problem : "";
    note.className = problem && input("btn-custom").value ? "small error" : "small muted";
    input("btn-plan").hidden = live.allowed;
    if (problem) {
      // Nothing to copy until the text is usable. The previews keep the last good text.
      code = "";
      input("btn-code").value = "";
      allow(false);
      return;
    }
    text = normaliseButtonText(typed);
    rememberText(slug, text);
    code = buttonSnippet({ href: bookingLink(slug), text, look: live.look, contrast });
    input("btn-code").value = code;
    drawButtonPreview(input("pv-light"), text, live.look);
    drawButtonPreview(input("pv-dark"), text, live.look);
    allow(true);
    input("btn-mail").setAttribute("href", developerEmail(code));
    input("btn-stale").hidden = !copiedCodeIsStale(lastCopied(slug), snippetFingerprint(code));
  }

  const say = (message) => {
    input("btn-msg").textContent = message;
  };

  input("btn-text").addEventListener("change", (e) => {
    own = e.target.value === OWN_TEXT;
    if (!own) text = e.target.value;
    input("btn-custom-row").hidden = !own;
    say("");
    if (own) {
      // Start from the words already on the button, so the owner edits it instead of retyping.
      if (!input("btn-custom").value) input("btn-custom").value = text;
      input("btn-custom").focus();
    }
    draw();
  });

  input("btn-custom").addEventListener("input", () => {
    say("");
    draw();
  });

  input("btn-copy").addEventListener("click", async () => {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      input("btn-code").select();
      say("Your browser wouldn't copy it. The code is selected: press Ctrl+C (or Cmd+C on a Mac) to copy it.");
      return;
    }
    rememberCopied(slug, snippetFingerprint(code));
    input("btn-stale").hidden = true;
    input("btn-copy").textContent = "Copied ✓";
    setTimeout(() => {
      input("btn-copy").textContent = "Copy code";
    }, 2500);
    say("Copied. Paste it where you want the button on your website.");
  });

  input("btn-mail").addEventListener("click", (e) => {
    if (!code) e.preventDefault();
  });

  return { draw };
}

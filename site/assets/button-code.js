// The "Button for your website" tab of the owner's Booking page screen: the owner picks the button's text, sees it,
// and copies one piece of code to paste into their own website, or emails it to whoever looks after that website.
// All text reaches the page through textContent. The code is built by look-model.js from fixed texts and checked colours.

import { el, fill } from "./gn.js";
import { contrast } from "./look.js";
import {
  BUTTON_TEXTS,
  buttonSnippet,
  buttonStyle,
  copiedCodeIsStale,
  developerEmail,
  snippetFingerprint,
} from "./look-model.js";
import { bookingLink } from "./owner.js";

const input = (id) => document.getElementById(id);

/**
 * The browser-storage key that remembers which code the owner last copied.
 * @param {string} slug The business slug.
 * @returns {string} The key.
 */
const storageKey = (slug) => `gn_button_${slug}`;

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
  let text = BUTTON_TEXTS[0];
  let code = "";
  let live = null;

  fill(input("btn-text"), ...BUTTON_TEXTS.map((t) => el("option", { value: t }, t)));

  /** Rebuild the code, the previews, the email link and the "copy again" note. */
  function draw(next) {
    if (next) live = next;
    if (!live) return;
    code = buttonSnippet({ href: bookingLink(slug), text, look: live.look, contrast });
    input("btn-code").value = code;
    drawButtonPreview(input("pv-light"), text, live.look);
    drawButtonPreview(input("pv-dark"), text, live.look);
    input("btn-plan").hidden = live.allowed;
    input("btn-mail").setAttribute("href", developerEmail(code));
    input("btn-stale").hidden = !copiedCodeIsStale(lastCopied(slug), snippetFingerprint(code));
  }

  const say = (message) => {
    input("btn-msg").textContent = message;
  };

  input("btn-text").addEventListener("change", (e) => {
    text = e.target.value;
    say("");
    draw();
  });

  input("btn-copy").addEventListener("click", async () => {
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

  return { draw };
}

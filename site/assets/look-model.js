// The booking page look as the owner's editor sees it: the standard look, the starting themes, and the checks that
// decide whether a look can be published. Plain data and functions with no page access, so they can be tested without
// a browser. The same rules are enforced again by the server, which has the last word.

/** Image types the server accepts for a logo. */
export const LOGO_TYPES = ["image/png", "image/jpeg", "image/webp"];

/** The largest logo, in bytes. The API refuses any request body over 64 KB, so the limit is 60 KB. */
export const LOGO_MAX_BYTES = 61440;

/** Font choices: the value the server stores and what the owner reads. */
export const FONTS = [
  ["modern", "Modern sans"],
  ["classic", "Classic serif"],
  ["rounded", "Friendly rounded"],
  ["geometric", "Geometric"],
];

/** Corner choices: the value the server stores and what the owner reads. */
export const CORNERS = [
  ["sharp", "Sharp"],
  ["soft", "Soft"],
  ["round", "Round"],
];

/** The page's own colours. This is also what customers see when a business has published nothing. */
export const STANDARD = Object.freeze({
  accent: "#d62c2d",
  background: "#070f1a",
  text: "#ffffff",
  font: "modern",
  corners: "soft",
  logo_id: null,
});

/** Starting points. Choosing one changes colours, font and corners, never the logo. */
export const THEMES = [
  { id: "standard", name: "Standard", ...STANDARD },
  {
    id: "midnight",
    name: "Midnight",
    accent: "#e0aa55",
    background: "#0c1726",
    text: "#f3f5f8",
    font: "modern",
    corners: "soft",
  },
  {
    id: "warm",
    name: "Warm",
    accent: "#b5562b",
    background: "#fbf5ec",
    text: "#3a2a1e",
    font: "classic",
    corners: "round",
  },
  {
    id: "fresh",
    name: "Fresh",
    accent: "#2f7d5b",
    background: "#f2f8f4",
    text: "#17322a",
    font: "rounded",
    corners: "round",
  },
  {
    id: "minimal",
    name: "Minimal",
    accent: "#111111",
    background: "#ffffff",
    text: "#111111",
    font: "geometric",
    corners: "sharp",
  },
  {
    id: "premium",
    name: "Premium",
    accent: "#c3985e",
    background: "#044233",
    text: "#f7f4ee",
    font: "classic",
    corners: "sharp",
  },
  {
    id: "classic",
    name: "Classic light",
    accent: "#13355a",
    background: "#ffffff",
    text: "#1c2733",
    font: "modern",
    corners: "soft",
  },
];

/** The look's fields, in the order the server takes them. */
const FIELDS = ["accent", "background", "text", "font", "corners", "logo_id"];

/**
 * The text colour for buttons and selected items: white or near-black, whichever reads better on the accent.
 * This is the server's rule, so the preview matches what customers get.
 * @param {string} accent The accent colour, #rrggbb.
 * @param {(a: string, b: string) => number} contrast The WCAG contrast ratio of two colours.
 * @returns {"#ffffff"|"#111111"} The text colour.
 */
export function onAccent(accent, contrast) {
  return contrast(accent, "#ffffff") >= contrast(accent, "#111111") ? "#ffffff" : "#111111";
}

/**
 * Say what stops a look being published. The thresholds are the server's: 4.5:1 for text and for button text, and 3:1
 * for the accent against the background.
 * @param {{accent: string, background: string, text: string}} look The look.
 * @param {(a: string, b: string) => number} contrast The WCAG contrast ratio of two colours.
 * @returns {string[]} The problems, in plain words. Empty when the look can be published.
 */
export function readabilityProblems(look, contrast) {
  const problems = [];
  const body = contrast(look.text, look.background);
  const button = contrast(onAccent(look.accent, contrast), look.accent);
  const accent = contrast(look.accent, look.background);
  if (body < 4.5) problems.push(`text on the background is hard to read (${body.toFixed(1)}:1, needs 4.5:1)`);
  if (button < 4.5) problems.push(`button text is hard to read (${button.toFixed(1)}:1, needs 4.5:1)`);
  if (accent < 3) problems.push(`highlights barely stand out from the background (${accent.toFixed(1)}:1, needs 3:1)`);
  return problems;
}

/**
 * Whether two looks are the same. A missing look is never the same as a look.
 * @param {object|null|undefined} a One look.
 * @param {object|null|undefined} b The other.
 * @returns {boolean} True when every field matches.
 */
export function sameLook(a, b) {
  if (!a || !b) return false;
  return FIELDS.every((field) => (a[field] ?? null) === (b[field] ?? null));
}

/**
 * Keep only the look's own fields from a server answer, which also carries the button text colour.
 * @param {object|null|undefined} look The look from the API.
 * @returns {object|null} The look, or null when there is none.
 */
export function fromServer(look) {
  if (!look) return null;
  return Object.fromEntries(FIELDS.map((field) => [field, look[field] ?? null]));
}

/**
 * The body the API takes when saving a draft.
 * @param {object} look The look.
 * @returns {object} Exactly the fields the API accepts, and nothing else.
 */
export function draftBody(look) {
  return Object.fromEntries(FIELDS.map((field) => [field, look[field] ?? null]));
}

/**
 * Work out what the editor should show and allow.
 * @param {object} state The working look, the saved draft, the published look (each or null), whether the plan
 *   includes branding, and the readability problems of the working look.
 * @returns {{status: "published"|"draft-saved"|"unsaved", canSave: boolean, canPublish: boolean, needsPlan: boolean}}
 *   The status line, and which buttons work. With no plan, publishing is replaced by a prompt to choose one.
 */
export function editorState({ working, draft, published, allowed, problems }) {
  const live = published || STANDARD;
  const changed = !sameLook(working, live);
  const saved = sameLook(working, draft);
  return {
    status: !changed ? "published" : saved ? "draft-saved" : "unsaved",
    canSave: changed && !saved,
    canPublish: changed && problems.length === 0,
    needsPlan: !allowed,
  };
}

/**
 * Check a chosen logo file before it is sent. The server checks the file's bytes as well.
 * @param {{type: string, size: number}} file The file.
 * @returns {string} What is wrong, or "" when it can be sent.
 */
export function logoProblem(file) {
  if (!LOGO_TYPES.includes(file.type)) return "That file type isn't allowed. Use a PNG, JPEG or WebP image.";
  if (file.size === 0) return "That image is empty.";
  if (file.size > LOGO_MAX_BYTES) {
    return `That image is too large. The limit is ${LOGO_MAX_BYTES / 1024} KB. Try a smaller or compressed version.`;
  }
  return "";
}

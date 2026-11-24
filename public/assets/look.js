// Applies a business's published look to a customer page.
//
// The look comes from our own API, which only stores colours as #rrggbb and fonts and corners from fixed sets. This
// module checks them again before use and only ever writes them as CSS custom properties, never as HTML or as a
// style sheet, so nothing a business enters can run as code. Any problem leaves the standard page untouched.

import { api } from "./gn.js";

const COLOUR = /^#[0-9a-f]{6}$/;
const LOGO_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Font stacks. "modern" is the page's own font, so the default look changes nothing. */
const FONTS = {
  modern: '"Montserrat", Helvetica, Arial, sans-serif',
  classic: 'Georgia, "Times New Roman", serif',
  rounded: 'ui-rounded, "Segoe UI Rounded", "Arial Rounded MT Bold", system-ui, sans-serif',
  geometric: '"Century Gothic", "Avenir Next", Futura, system-ui, sans-serif',
};

/** Corner radii as [panels and small items, large cards, buttons]. "soft" is the page's own shape. */
const CORNERS = {
  sharp: ["2px", "4px", "2px"],
  soft: ["8px", "16px", "10px"],
  round: ["16px", "28px", "24px"],
};

/**
 * Relative luminance of a #rrggbb colour (WCAG 2).
 * @param {string} hex The colour.
 * @returns {number} Its luminance, from 0 to 1.
 */
function luminance(hex) {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * Contrast ratio of two #rrggbb colours (WCAG 2).
 * @param {string} a One colour.
 * @param {string} b The other colour.
 * @returns {number} The ratio, from 1 to 21.
 */
export function contrast(a, b) {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

/**
 * A colour mixed with another, in CSS.
 * @param {string} colour The main colour.
 * @param {number} percent How much of it, from 0 to 100.
 * @param {string} other The colour it is mixed into, such as "transparent".
 * @returns {string} A CSS colour.
 */
function mix(colour, percent, other) {
  return `color-mix(in srgb, ${colour} ${percent}%, ${other})`;
}

/**
 * Turn a look from the API into the page's colour and shape roles.
 * @param {any} look The look, as the public API gives it.
 * @returns {Record<string, string>|null} CSS custom properties, or null if the look is not what we expect.
 */
export function lookProperties(look) {
  if (!look || typeof look !== "object") return null;
  const { accent, background, text, on_accent: onAccent, font, corners } = look;
  if (![accent, background, text].every((c) => typeof c === "string" && COLOUR.test(c))) return null;
  if (onAccent !== "#ffffff" && onAccent !== "#111111") return null;
  if (!Object.hasOwn(FONTS, font) || !Object.hasOwn(CORNERS, corners)) return null;
  const [radius, radiusLarge, radiusButton] = CORNERS[corners];
  // Small text in the accent needs more contrast than the 3:1 that publishing guarantees, so links fall back to the
  // text colour when the accent is too faint to read on the background.
  const link = contrast(accent, background) >= 4.5 ? accent : text;
  const hoverTowards = onAccent === "#ffffff" ? "#000000" : "#ffffff";
  return {
    "--page-bg": background,
    "--glow": "none",
    "--text": text,
    "--muted": mix(text, 82, "transparent"),
    "--panel": mix(text, 2.5, "transparent"),
    "--line": mix(text, 8, "transparent"),
    "--line-strong": mix(text, 14, "transparent"),
    "--edge": mix(text, 10, "transparent"),
    "--edge-strong": mix(text, 24, "transparent"),
    "--edge-ghost": mix(text, 30, "transparent"),
    "--edge-faint": mix(text, 5, "transparent"),
    "--disabled-fg": mix(text, 45, "transparent"),
    "--input-bg": mix(text, 6, "transparent"),
    "--font": FONTS[font],
    "--radius": radius,
    "--radius-lg": radiusLarge,
    "--radius-btn": radiusButton,
    "--action": accent,
    "--on-action": onAccent,
    "--action-hover": mix(accent, 86, hoverTowards),
    "--action-glow": mix(accent, 60, "transparent"),
    "--action-glow-soft": mix(accent, 30, "transparent"),
    "--select": accent,
    "--on-select": onAccent,
    "--on-select-sub": mix(onAccent, 88, accent),
    "--link": link,
    "--link-hover": text,
    "--focus": accent,
    "--focus-glow": mix(accent, 25, "transparent"),
    "--card-bg": text,
    "--card-fg": background,
    "--card-sub": mix(background, 75, text),
    "--card-warn": contrast(accent, text) >= 4.5 ? accent : background,
    "--notice-bg": mix(accent, 16, "transparent"),
    "--tick-glow": mix(accent, 16, "transparent"),
  };
}

/**
 * Fetch and apply a business's published look, then let the page show itself.
 * @param {string} slug The business's public identifier.
 * @returns {Promise<string|null>} The address of the business's logo, or null for none. Null as well when the look
 *   is the standard one or could not be loaded, which leaves the page as it always was.
 */
export async function applyLook(slug) {
  const answer = await api(`/businesses/${encodeURIComponent(slug)}/look`);
  const look = answer.ok && answer.data ? answer.data.look : null;
  const properties = lookProperties(look);
  document.documentElement.classList.remove("look-pending");
  if (!properties) return null;
  for (const [name, value] of Object.entries(properties)) document.documentElement.style.setProperty(name, value);
  const logo = typeof look.logo_id === "string" && LOGO_ID.test(look.logo_id) ? look.logo_id : null;
  return logo ? `/businesses/${encodeURIComponent(slug)}/logos/${logo}` : null;
}

// Shared helpers for the owner sign-up and sign-in pages. All text reaches the page through textContent.

import { AUTH_KEY, AUTH_URL } from "./config.js";

/** The owner dashboard, at the site root. */
export const DASHBOARD = `${location.origin}/`;

/** A plausible email address. */
export const EMAIL = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$/;

/** Whether config.js still holds the example values. */
export const NOT_CONFIGURED = AUTH_KEY.includes("your-key");

/**
 * Call the identity provider.
 * @param {string} path The path after /auth/v1, starting with "/".
 * @param {object} body The JSON body.
 * @param {{method?: string, token?: string}} [options] The method (POST by default) and a bearer token.
 * @returns {Promise<{status: number, data: any}>} The answer. status 0 means no answer.
 */
export async function auth(path, body, options = {}) {
  const headers = { apikey: AUTH_KEY, "Content-Type": "application/json" };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  try {
    const response = await fetch(AUTH_URL + path, {
      method: options.method || "POST",
      headers,
      body: JSON.stringify(body),
    });
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    return { status: response.status, data };
  } catch {
    return { status: 0, data: {} };
  }
}

/**
 * Show or clear one field's error. The error element has the id "<field id>-err".
 * @param {string} id The field id.
 * @param {string} message The error, or "" to clear it.
 */
export function setError(id, message) {
  const error = document.getElementById(`${id}-err`);
  const control = document.getElementById(id);
  error.textContent = message;
  error.hidden = !message;
  control.setAttribute("aria-invalid", message ? "true" : "false");
  if (message) control.setAttribute("aria-describedby", `${id}-err`);
  else control.removeAttribute("aria-describedby");
}

/**
 * The region each sign-up time zone belongs to. Matches the regions the server offers.
 * Provisional: the region list may change; update this map with it.
 */
const ZONE_REGIONS = { "Europe/London": "uk", "Asia/Dubai": "ae", "Asia/Riyadh": "sa", "Asia/Qatar": "qa" };

/**
 * Guess the business's region from this device's time zone, to pre-select it. The owner confirms it.
 * Nothing is sent anywhere and no permission is asked.
 * @returns {string} The region, or "" when the device's time zone is not one of the regions.
 */
export function regionFromDevice() {
  try {
    return ZONE_REGIONS[Intl.DateTimeFormat().resolvedOptions().timeZone] || "";
  } catch {
    return "";
  }
}

/**
 * Show a message in a box, or hide the box.
 * @param {string} id The box id.
 * @param {string} message The message, or "".
 */
export function say(id, message) {
  const box = document.getElementById(id);
  box.textContent = message;
  box.hidden = !message;
}

/**
 * Wire a Show/Hide button to a password field.
 * @param {string} buttonId The button id.
 * @param {string} fieldId The password field id.
 */
export function passwordToggle(buttonId, fieldId) {
  const button = document.getElementById(buttonId);
  button.addEventListener("click", () => {
    const field = document.getElementById(fieldId);
    const show = field.type === "password";
    field.type = show ? "text" : "password";
    button.textContent = show ? "Hide" : "Show";
    button.setAttribute("aria-pressed", String(show));
  });
}

/**
 * Keep a new sign-in for this browser tab and open the owner's Today screen.
 * The session is saved where the existing owner screens also read it, so the menu links keep working.
 * @param {{access_token: string, refresh_token?: string}} session The identity provider's session.
 * @param {string} email The email the owner signed in with.
 */
export function startOwnerSession(session, email) {
  try {
    sessionStorage.setItem(
      "gn_owner_session",
      JSON.stringify({ access: session.access_token, refresh: session.refresh_token || "", email }),
    );
  } catch {
    // Storage unavailable: hand the session over in the address instead.
    openDashboard(session, "signin");
    return;
  }
  location.assign("/app/");
}

/**
 * Open the dashboard with a session, the same way the sign-up confirmation link does.
 * The tokens travel after "#", so they never reach a server, and the dashboard removes them on arrival.
 * @param {{access_token: string, refresh_token?: string}} session The identity provider's session.
 * @param {string} type How the session was made, for example "signin" or "signup".
 */
export function openDashboard(session, type) {
  const hash = new URLSearchParams({
    access_token: session.access_token,
    refresh_token: session.refresh_token || "",
    type,
  });
  location.assign(`${DASHBOARD}#${hash}`);
}

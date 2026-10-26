// The site root. It holds no page of its own: it sends each kind of link that lands here to the right page.
// Links land here from sign-up confirmation emails, password reset emails, Stripe's return after set-up or
// payment, and older booking and manage links. Tokens after "#" are never sent to a server.

import { BOOKING_URL } from "./config.js";

const SESSION_KEY = "gn_owner_session";
const hashText = location.hash.slice(1);
const hash = new URLSearchParams(hashText);
const query = new URLSearchParams(location.search);

/**
 * Read the email from a session token's claims.
 * @param {string} token The access token.
 * @returns {string} The email, or "".
 */
function emailFrom(token) {
  try {
    let part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    while (part.length % 4) part += "=";
    const bytes = Uint8Array.from(atob(part), (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)).email || "";
  } catch {
    return "";
  }
}

/** @returns {boolean} Whether this tab has a signed-in owner. */
function signedIn() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
    return Boolean(saved && saved.access);
  } catch {
    return false;
  }
}

/** @returns {string} Where to go. */
function destination() {
  const business = query.get("b") || "";
  if (/^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$/.test(business)) {
    return `${BOOKING_URL}/book/${business}${/^(paid|unpaid)=/.test(hashText) ? `#${hashText}` : ""}`;
  }
  if (hashText.startsWith("manage=")) return `${BOOKING_URL}/manage/#${hashText.slice("manage=".length)}`;

  const token = hash.get("access_token");
  const type = hash.get("type") || "";
  if (token && type === "recovery") return `/signin/#${hashText}`;
  if (token && /^(signup|email|magiclink|signin)$/.test(type)) {
    try {
      sessionStorage.setItem(
        SESSION_KEY,
        JSON.stringify({ access: token, refresh: hash.get("refresh_token") || "", email: emailFrom(token) }),
      );
    } catch {
      return "/signin/";
    }
    return "/app/";
  }
  if (hash.get("error") || hash.get("error_description")) return "/signin/?link=expired";

  const billing = /^billing=(done|cancelled|portal)$/.exec(hashText);
  if (billing) return `/app/plan/#billing=${billing[1]}`;
  const payments = /^payments=(done|retry)$/.exec(hashText);
  if (payments) return `/app/payments/#payments=${payments[1]}`;

  if (hashText === "signin") return "/signin/";
  const go = { "go=today": "/app/", "go=services": "/app/services/", "go=payments": "/app/payments/" }[hashText];
  if (go) return go;

  return signedIn() ? "/app/" : "/home/";
}

location.replace(destination());

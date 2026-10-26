// Shared parts of the owner screens: the signed-in session, calls to the owner API, and the page frame
// (sidebar, phone menu and plan card). All text reaches the page through textContent.

import { auth } from "./auth.js";
import { API_URL, BOOKING_URL } from "./config.js";
import { el, fill, money } from "./gn.js";

const SESSION_KEY = "gn_owner_session";
const PLAN_NAMES = { free: "Free", starter: "Starter", pro: "Pro" };
const TRIAL_DAYS = 14;

/** The owner menu: label, address and the key a page passes to mark itself current. */
const MENU = [
  ["today", "Today", "/app/"],
  ["bookings", "Bookings", "/app/bookings/"],
  ["services", "Services", "/app/services/"],
  ["hours", "Opening hours", "/app/hours/"],
  ["payments", "Payments", "/app/payments/"],
  ["settings", "Settings", "/app/settings/"],
];

/** The shorter phone menu. */
const PHONE_MENU = ["today", "bookings", "services", "settings"];

let session = null;

/** @returns {object|null} The saved session for this tab, or null. */
export function readSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
    return saved && saved.access ? saved : null;
  } catch {
    return null;
  }
}

/**
 * Save the session for this tab.
 * @param {object|null} value The session, or null to clear it.
 */
export function saveSession(value) {
  session = value;
  try {
    if (value) sessionStorage.setItem(SESSION_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Storage unavailable: the session lasts until the page closes.
  }
}

/** Leave for the sign-in page. */
export function toSignIn() {
  saveSession(null);
  location.replace("/signin/");
}

/** @returns {Promise<boolean>} Whether the session could be renewed. */
async function renew() {
  if (!session || !session.refresh) return false;
  const result = await auth("/token?grant_type=refresh_token", { refresh_token: session.refresh });
  if (result.status !== 200 || !result.data.access_token) return false;
  saveSession({ access: result.data.access_token, refresh: result.data.refresh_token, email: session.email });
  return true;
}

/**
 * Call the owner API, renewing the session once if it has expired. A non-GET call always sends a JSON body,
 * because the API's host refuses a body-less POST.
 * @param {string} path The path after /owner.
 * @param {{method?: string, body?: object}} [options] The method and JSON body.
 * @returns {Promise<{status: number, data: any}>} The answer. status 0 means no answer.
 */
export async function owner(path, options = {}) {
  const method = options.method || "GET";
  const call = async () => {
    const init = { method, headers: { Authorization: `Bearer ${session.access}` } };
    if (method !== "GET") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body || {});
    }
    try {
      const response = await fetch(`${API_URL}/owner${path}`, init);
      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      return { status: response.status, data: body };
    } catch {
      return { status: 0, data: null };
    }
  };
  let answer = await call();
  if (answer.status === 401 && (await renew())) answer = await call();
  if (answer.status === 401) toSignIn();
  return answer;
}

/**
 * Read the public answer for a business: its currency, time zone, payment options and booking window.
 * @param {string} slug The business slug.
 * @returns {Promise<any>} The business part of the answer, or null.
 */
export async function publicBusiness(slug) {
  try {
    const response = await fetch(`${API_URL}/businesses/${slug}/services`);
    const body = response.ok ? await response.json() : null;
    return body && body.business ? body.business : null;
  } catch {
    return null;
  }
}

/**
 * The business's booking page address.
 * @param {string} slug The business slug.
 * @returns {string} The address.
 */
export function bookingLink(slug) {
  return `${BOOKING_URL}/book/${slug}`;
}

/**
 * Show a message in a box, or hide it.
 * @param {string} id The box id.
 * @param {string} message The message, or "".
 * @param {boolean} [good] Whether it is good news (otherwise a warning).
 */
export function notice(id, message, good = false) {
  const box = document.getElementById(id);
  box.textContent = message;
  box.hidden = !message;
  box.className = good ? "notice" : "notice warn";
}

/**
 * Describe the business's plan for the plan card and plan screen.
 * @param {object} b The business from /owner/me.
 * @returns {{name: string, note: string, fraction: number, trialLeft: number}} The description.
 */
export function describePlan(b) {
  const now = new Date();
  const trialLeft = b.trial_ends_at ? Math.ceil((new Date(b.trial_ends_at) - now) / 86400000) : 0;
  const onTrial = b.plan === "free" && trialLeft > 0;
  const name = onTrial ? "Starter trial" : PLAN_NAMES[b.plan] || b.plan;
  if (onTrial) {
    return {
      name,
      note: `${trialLeft} ${trialLeft === 1 ? "day" : "days"} left. No card needed until you choose.`,
      fraction: Math.min(1, trialLeft / TRIAL_DAYS),
      trialLeft,
    };
  }
  if (b.plan_status === "past_due") {
    return {
      name,
      note: "Your last payment didn't go through. Update your card to keep your plan.",
      fraction: 1,
      trialLeft,
    };
  }
  if (b.plan_ends_at) {
    const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long" }).format(new Date(b.plan_ends_at));
    return { name, note: `Cancelled. You keep it until ${day}, then move to Free.`, fraction: 1, trialLeft };
  }
  if (b.plan === "free") {
    return {
      name,
      note: b.trial_ends_at ? "Your trial has ended. Upgrade any time." : "Upgrade any time.",
      fraction: 0,
      trialLeft,
    };
  }
  return { name, note: "Thanks for being on a paid plan.", fraction: 1, trialLeft };
}

/**
 * Draw the sidebar and phone menu.
 * @param {string} current The key of the current page.
 * @param {object} b The business from /owner/me.
 */
function drawFrame(current, b) {
  const plan = describePlan(b);
  const link = (key, label, href) =>
    el(
      "a",
      { href, class: key === current ? "on" : null, "aria-current": key === current ? "page" : null },
      el("i", { "aria-hidden": "true" }),
      label,
    );
  const bar = el("i");
  bar.style.width = `${Math.round(plan.fraction * 100)}%`;
  const signOut = el("button", { type: "button", class: "signout" }, "Sign out");
  signOut.addEventListener("click", async () => {
    if (session) await auth("/logout", {}, { token: session.access });
    toSignIn();
  });
  fill(
    document.getElementById("side"),
    el("a", { class: "brand", href: "/app/" }, "GRIT", el("span", {}, "NATIONS")),
    el(
      "div",
      { class: "biz" },
      el("span", { class: "biz-badge", "aria-hidden": "true" }, (b.name.trim()[0] || "?").toUpperCase()),
      el("span", { class: "biz-text" }, el("strong", {}, b.name), el("span", {}, plan.name)),
    ),
    el(
      "nav",
      { class: "menu", "aria-label": "Main menu" },
      ...MENU.map(([key, label, href]) => link(key, label, href)),
    ),
    el(
      "section",
      { class: "plan-card", "aria-label": "Your plan" },
      el("span", { class: "eyebrow" }, "Your plan"),
      el("strong", {}, plan.name),
      el("span", { class: "bar", "aria-hidden": "true" }, bar),
      el("span", { class: "small muted" }, plan.note),
      el("a", { class: "small", href: "/app/plan/" }, b.plan === "free" ? "Choose a plan →" : "Manage your plan →"),
    ),
    signOut,
  );
  fill(
    document.getElementById("bottom"),
    ...MENU.filter(([key]) => PHONE_MENU.includes(key)).map(([key, label, href]) =>
      el("a", { href, class: key === current ? "on" : null, "aria-current": key === current ? "page" : null }, label),
    ),
  );
}

/**
 * Start an owner page: check the session, load the business and draw the frame.
 * Without a session it goes to sign-in; without a business it goes to the set-up screen.
 * @param {string} current The key of the current page, or "" for pages outside the menu.
 * @returns {Promise<{business: object, slug: string}|null>} The business, or null when the page is leaving.
 */
export async function startOwnerPage(current) {
  session = readSession();
  if (!session) {
    toSignIn();
    return null;
  }
  const me = await owner("/me");
  if (me.status !== 200 || !me.data || !Array.isArray(me.data.businesses)) {
    if (me.status !== 401) notice("app-msg", "This page couldn't load. Check your connection and refresh the page.");
    return null;
  }
  if (!me.data.businesses.length) {
    location.replace("/app/setup/");
    return null;
  }
  const business = me.data.businesses[0];
  drawFrame(current, business);
  return { business, slug: business.slug };
}

/**
 * The chip for a booking's payment state.
 * @param {object} b The booking.
 * @param {string} currency ISO 4217 code.
 * @returns {HTMLElement} The chip.
 */
export function bookingChip(b, currency) {
  const price = money(b.price_minor, currency);
  if (b.status === "cancelled") return el("span", { class: "chip cancelled" }, "Cancelled");
  if (b.status === "pending_payment" || b.payment_status === "awaiting") {
    return el("span", { class: "chip waiting" }, "Awaiting payment");
  }
  if (b.payment_status === "paid") return el("span", { class: "chip online" }, `Paid online ${price}`);
  if (b.payment_status === "at_venue") return el("span", { class: "chip venue" }, `Pay at venue ${price}`);
  return el("span", { class: "chip free" }, b.price_minor ? price : "Free");
}

/** @returns {string} The email of the signed-in owner, or "". */
export function sessionEmail() {
  return (session && session.email) || "";
}

/** @returns {string} The access token of the signed-in owner, or "". */
export function accessToken() {
  return (session && session.access) || "";
}

/** Load the current session without drawing anything, for pages that only need to call the API. */
export function useSession() {
  session = readSession();
  return session;
}

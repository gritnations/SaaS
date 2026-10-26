// Owner Today screen: set-up checklist, upcoming and cancelled bookings, the booking link and this week's figures.
// Reads the signed-in session the sign-in page saved for this tab. All text reaches the page through textContent.

import { auth } from "./auth.js";
import { API_URL, BOOKING_URL } from "./config.js";
import { addDays, dateParts, el, fill, money, today } from "./gn.js";

const SESSION_KEY = "gn_owner_session";
const PLAN_NAMES = { free: "Free", starter: "Starter", pro: "Pro" };
const TRIAL_DAYS = 14;
const input = (id) => document.getElementById(id);

let session = null;
let tab = "upcoming";
const data = { business: null, currency: "GBP", zone: "Europe/London", bookings: [] };

/** @returns {object|null} The saved session for this tab, or null. */
function readSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
    return saved && saved.access ? saved : null;
  } catch {
    return null;
  }
}

/**
 * Save the session for this tab, where the existing owner screens also read it.
 * @param {object|null} value The session, or null to clear it.
 */
function saveSession(value) {
  session = value;
  try {
    if (value) sessionStorage.setItem(SESSION_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Storage unavailable: the session lasts until the page closes.
  }
}

/** Leave for the sign-in page. */
function toSignIn() {
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
 * Read from the owner API, renewing the session once if it has expired.
 * @param {string} path The path after /owner.
 * @returns {Promise<{status: number, data: any}>} The answer. status 0 means no answer.
 */
async function owner(path) {
  const call = async () => {
    try {
      const response = await fetch(`${API_URL}/owner${path}`, {
        headers: { Authorization: `Bearer ${session.access}` },
      });
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
 * Read a public business answer.
 * @param {string} slug The business slug.
 * @returns {Promise<any>} The answer, or null.
 */
async function publicBusiness(slug) {
  try {
    const response = await fetch(`${API_URL}/businesses/${slug}/services`);
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

/**
 * Show a problem at the top of the page.
 * @param {string} message The message.
 */
function problem(message) {
  input("app-msg").textContent = message;
  input("app-msg").hidden = false;
}

/** @returns {string} The business's booking page address. */
function bookingLink() {
  return `${BOOKING_URL}/book/${data.business.slug}`;
}

/** Fill the header, sidebar business and plan card. */
function showBusiness() {
  const b = data.business;
  document.title = `Today · ${b.name}`;
  input("biz-name").textContent = b.name;
  input("biz-badge").textContent = (b.name.trim()[0] || "?").toUpperCase();
  const now = new Date();
  const hour = Number(
    new Intl.DateTimeFormat("en-GB", { timeZone: data.zone, hour: "2-digit", hourCycle: "h23" }).format(now),
  );
  input("greeting").textContent = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  input("today-date").textContent = dateParts(today(data.zone)).long;
  input("open-page").href = bookingLink();

  const trialLeft = b.trial_ends_at ? Math.ceil((new Date(b.trial_ends_at) - now) / 86400000) : 0;
  const onTrial = b.plan === "free" && trialLeft > 0;
  const name = onTrial ? "Starter trial" : PLAN_NAMES[b.plan] || b.plan;
  input("biz-plan").textContent = name;
  input("plan-name").textContent = name;
  let note;
  let fraction = 0;
  if (onTrial) {
    note = `${trialLeft} ${trialLeft === 1 ? "day" : "days"} left. No card needed until you choose.`;
    fraction = Math.min(1, trialLeft / TRIAL_DAYS);
  } else if (b.plan_status === "past_due") {
    note = "Your last payment didn't go through. Update your card to keep your plan.";
  } else if (b.plan_ends_at) {
    note = `Your plan ends on ${new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long" }).format(new Date(b.plan_ends_at))}.`;
    fraction = 1;
  } else if (b.plan === "free") {
    note = "Upgrade any time for more.";
  } else {
    note = "Thanks for being on a paid plan.";
    fraction = 1;
  }
  input("plan-note").textContent = note;
  input("plan-bar").style.width = `${Math.round(fraction * 100)}%`;
  input("plan-link").textContent = b.plan === "free" ? "Choose a plan →" : "Manage your plan →";
}

/**
 * Build the set-up checklist.
 * @param {{services: any, hours: any, payments: any}} parts The owner answers.
 */
function showSetup(parts) {
  const shared = (() => {
    try {
      return localStorage.getItem(`gn_shared_${data.business.slug}`) === "1";
    } catch {
      return false;
    }
  })();
  const steps = [
    {
      name: "Add your services",
      note: "Names, length and price",
      done: Array.isArray(parts.services) && parts.services.some((s) => s.active),
      href: "/#go=services",
    },
    {
      name: "Set opening hours",
      note: "The days and times you take bookings",
      done: Boolean(parts.hours && parts.hours.blocks && parts.hours.blocks.length),
      href: "/#go=services",
    },
    { name: "Share your link", note: "Copy it or send it to customers", done: shared, href: "#share" },
    {
      name: "Take card payments (optional)",
      note: "Connect Stripe for online payments",
      done: Boolean(parts.payments && parts.payments.connected && parts.payments.enabled),
      href: "/#go=payments",
    },
  ];
  const doneCount = steps.filter((s) => s.done).length;
  if (doneCount === steps.length) {
    input("setup").hidden = true;
    return;
  }
  input("setup-count").textContent = `${doneCount} of ${steps.length} done`;
  fill(input("setup-bar"), ...steps.map((s) => el("i", { class: s.done ? "on" : "" })));
  fill(
    input("setup-steps"),
    ...steps.map((s) =>
      el(
        "li",
        {},
        el(
          "a",
          { class: s.done ? "step is-done" : "step", href: s.href },
          el("span", { class: "st-tick", "aria-hidden": "true" }, s.done ? "✓" : ""),
          el("strong", {}, s.name),
          s.done ? el("span", { class: "visually-hidden" }, "Done") : el("span", { class: "note" }, s.note),
        ),
      ),
    ),
  );
  input("setup").hidden = false;
}

/**
 * The chip for a booking's payment state.
 * @param {object} b The booking.
 * @returns {HTMLElement} The chip.
 */
function chip(b) {
  const price = money(b.price_minor, data.currency);
  if (b.status === "cancelled") return el("span", { class: "chip cancelled" }, "Cancelled");
  if (b.status === "pending_payment" || b.payment_status === "awaiting")
    return el("span", { class: "chip waiting" }, "Awaiting payment");
  if (b.payment_status === "paid") return el("span", { class: "chip online" }, `Paid online ${price}`);
  if (b.payment_status === "at_venue") return el("span", { class: "chip venue" }, `Pay at venue ${price}`);
  return el("span", { class: "chip free" }, b.price_minor ? price : "Free");
}

/**
 * The label for a day heading.
 * @param {string} iso The date.
 * @returns {string} For example "Today · Mon 26".
 */
function dayLabel(iso) {
  const p = dateParts(iso);
  const short = `${p.dow} ${p.day}`;
  const now = today(data.zone);
  if (iso === now) return `Today · ${short}`;
  if (iso === addDays(now, 1)) return `Tomorrow · ${short}`;
  return `${p.dow} ${p.day} ${p.month}`;
}

/** Draw the bookings list for the current tab. */
function showBookings() {
  input("tab-upcoming").setAttribute("aria-selected", String(tab === "upcoming"));
  input("tab-cancelled").setAttribute("aria-selected", String(tab === "cancelled"));
  input("bk-h").textContent = tab === "upcoming" ? "Upcoming bookings" : "Cancelled bookings";
  const now = Date.now();
  const rows =
    tab === "upcoming"
      ? data.bookings.filter((b) => b.status !== "cancelled" && b.status !== "expired" && new Date(b.ends_at) > now)
      : data.bookings.filter((b) => b.status === "cancelled");
  if (!rows.length) {
    fill(
      input("bk-list"),
      el(
        "p",
        { class: "pad muted" },
        tab === "upcoming"
          ? "No upcoming bookings in the next two weeks. Share your link to get your first one."
          : "No cancelled bookings in these two weeks.",
      ),
    );
    return;
  }
  const parts = [];
  let current = "";
  for (const b of rows.slice(0, 20)) {
    if (b.date !== current) {
      current = b.date;
      parts.push(el("div", { class: "day-label" }, dayLabel(b.date)));
    }
    parts.push(
      el(
        "div",
        { class: "bk-row" },
        el("span", { class: "bk-time" }, b.time),
        el("span", { class: "who" }, el("strong", {}, b.customer_name), el("span", {}, b.service_name)),
        el("span", { class: "meta" }, chip(b), el("span", { class: "ref" }, b.reference)),
      ),
    );
  }
  fill(input("bk-list"), ...parts);
}

/**
 * Fill this week's figures.
 * @param {string} monday The first day of this week.
 */
function showWeek(monday) {
  const sunday = addDays(monday, 6);
  const week = data.bookings.filter((b) => b.date >= monday && b.date <= sunday);
  const live = week.filter((b) => b.status === "confirmed");
  const sum = (list) => list.reduce((total, b) => total + b.price_minor, 0);
  input("st-count").textContent = String(live.length);
  input("st-paid").textContent = money(sum(live.filter((b) => b.payment_status === "paid")), data.currency).replace(
    "Free",
    "£0",
  );
  input("st-venue").textContent = money(
    sum(live.filter((b) => b.payment_status === "at_venue")),
    data.currency,
  ).replace("Free", "£0");
  input("st-cancelled").textContent = String(week.filter((b) => b.status === "cancelled").length);
}

/** Remember that the owner has shared the link, and tick the step. */
function markShared() {
  try {
    localStorage.setItem(`gn_shared_${data.business.slug}`, "1");
  } catch {
    // Storage unavailable: the step stays unticked.
  }
}

/** Wire the share card. */
function wireShare() {
  const link = bookingLink();
  input("share-link").textContent = link.replace(/^https:\/\//, "");
  input("share-open").href = link;
  const say = (text) => {
    input("share-msg").textContent = text;
  };
  input("copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(link);
      input("copy").textContent = "Copied ✓";
      say("Paste it into Instagram, Google or your website.");
      markShared();
    } catch {
      say(`Copy this link: ${link}`);
    }
  });
  input("share-btn").addEventListener("click", async () => {
    if (navigator.share) {
      try {
        await navigator.share({ title: data.business.name, text: `Book with ${data.business.name}`, url: link });
        markShared();
      } catch {
        // The owner closed the share sheet.
      }
    } else {
      input("copy").click();
    }
  });
  input("share-open").addEventListener("click", markShared);
}

/** Sign out of this tab and the identity provider. */
async function signOut() {
  if (session) await auth("/logout", {}, { token: session.access });
  toSignIn();
}

/** Load everything and draw the page. */
async function start() {
  session = readSession();
  if (!session) {
    toSignIn();
    return;
  }
  input("signout").addEventListener("click", signOut);
  const me = await owner("/me");
  if (me.status !== 200) {
    if (me.status !== 401) problem("Your dashboard couldn't load. Check your connection and refresh the page.");
    return;
  }
  if (!me.data.businesses.length) {
    // No business yet: the existing set-up screen creates it.
    location.replace("/#go=today");
    return;
  }
  data.business = me.data.businesses[0];
  const slug = data.business.slug;
  const pub = await publicBusiness(slug);
  if (pub) {
    data.currency = pub.business.currency;
    data.zone = pub.business.time_zone;
  }
  showBusiness();
  wireShare();

  const now = today(data.zone);
  const weekday = (new Date(`${now}T12:00:00Z`).getUTCDay() + 6) % 7;
  const monday = addDays(now, -weekday);
  const [bookings, services, hours, payments] = await Promise.all([
    owner(`/businesses/${slug}/bookings?start=${monday}&days=14`),
    owner(`/businesses/${slug}/services`),
    owner(`/businesses/${slug}/hours`),
    owner(`/businesses/${slug}/payments`),
  ]);
  if (bookings.status === 200) {
    data.zone = bookings.data.time_zone || data.zone;
    data.bookings = bookings.data.bookings;
    showBookings();
    showWeek(monday);
  } else {
    fill(input("bk-list"), el("p", { class: "pad muted" }, "Bookings couldn't load. Refresh the page to try again."));
  }
  showSetup({
    services: services.status === 200 ? services.data : null,
    hours: hours.status === 200 ? hours.data : null,
    payments: payments.status === 200 ? payments.data : null,
  });
}

input("tab-upcoming").addEventListener("click", () => {
  tab = "upcoming";
  showBookings();
});
input("tab-cancelled").addEventListener("click", () => {
  tab = "cancelled";
  showBookings();
});
start();

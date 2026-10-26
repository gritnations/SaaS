// Owner Today screen: set-up checklist, upcoming and cancelled bookings, the booking link and this week's figures.
// All text reaches the page through textContent.

import { addDays, dateParts, el, fill, money, today } from "./gn.js";
import { bookingChip, bookingLink, notice, owner, publicBusiness, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
let tab = "upcoming";
const data = { slug: "", name: "", currency: "GBP", zone: "Europe/London", bookings: [] };

/**
 * Format an amount, showing a zero total as a zero amount rather than "Free".
 * @param {number} minor Amount in minor units.
 * @returns {string} The amount.
 */
function total(minor) {
  if (minor) return money(minor, data.currency);
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: data.currency,
    maximumFractionDigits: 0,
  }).format(0);
}

/** Fill the greeting, date and booking page link. */
function showHeader() {
  const hour = Number(
    new Intl.DateTimeFormat("en-GB", { timeZone: data.zone, hour: "2-digit", hourCycle: "h23" }).format(new Date()),
  );
  input("head-title").textContent = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  input("head-sub").textContent = dateParts(today(data.zone)).long;
  input("open-page").href = bookingLink(data.slug);
  document.title = `Today · ${data.name}`;
}

/**
 * Build the set-up checklist.
 * @param {{services: any, hours: any, payments: any}} parts The owner answers, or null where they failed.
 */
function showSetup(parts) {
  let shared;
  try {
    shared = localStorage.getItem(`gn_shared_${data.slug}`) === "1";
  } catch {
    shared = false;
  }
  const steps = [
    {
      name: "Add your services",
      note: "Names, length and price",
      done: Array.isArray(parts.services) && parts.services.some((s) => s.active),
      href: "/app/services/",
    },
    {
      name: "Set opening hours",
      note: "The days and times you take bookings",
      done: Boolean(parts.hours && Array.isArray(parts.hours.blocks) && parts.hours.blocks.length),
      href: "/app/hours/",
    },
    { name: "Share your link", note: "Copy it or send it to customers", done: shared, href: "#share" },
    {
      name: "Take card payments (optional)",
      note: "Connect Stripe for online payments",
      done: Boolean(parts.payments && parts.payments.connected && parts.payments.enabled),
      href: "/app/payments/",
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
 * The label for a day heading.
 * @param {string} iso The date.
 * @returns {string} For example "Today · Mon 26".
 */
function dayLabel(iso) {
  const p = dateParts(iso);
  const now = today(data.zone);
  if (iso === now) return `Today · ${p.dow} ${p.day}`;
  if (iso === addDays(now, 1)) return `Tomorrow · ${p.dow} ${p.day}`;
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
        "a",
        { class: "bk-row link-row", href: `/app/bookings/#${b.reference}` },
        el("span", { class: "bk-time" }, b.time),
        el("span", { class: "who" }, el("strong", {}, b.customer_name), el("span", {}, b.service_name)),
        el("span", { class: "meta" }, bookingChip(b, data.currency), el("span", { class: "ref" }, b.reference)),
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
  const sum = (list) => list.reduce((acc, b) => acc + b.price_minor, 0);
  input("st-count").textContent = String(live.length);
  input("st-paid").textContent = total(sum(live.filter((b) => b.payment_status === "paid")));
  input("st-venue").textContent = total(sum(live.filter((b) => b.payment_status === "at_venue")));
  input("st-cancelled").textContent = String(week.filter((b) => b.status === "cancelled").length);
}

/** Remember that the owner has shared the link. */
function markShared() {
  try {
    localStorage.setItem(`gn_shared_${data.slug}`, "1");
  } catch {
    // Storage unavailable: the step stays unticked.
  }
}

/** Wire the share card. */
function wireShare() {
  const link = bookingLink(data.slug);
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
        await navigator.share({ title: data.name, text: `Book with ${data.name}`, url: link });
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

/** Load everything and draw the page. */
async function start() {
  const page = await startOwnerPage("today");
  if (!page) return;
  data.slug = page.slug;
  data.name = page.business.name;
  const pub = await publicBusiness(data.slug);
  if (pub) {
    data.currency = pub.currency || data.currency;
    data.zone = pub.time_zone || data.zone;
  }
  showHeader();
  wireShare();

  const now = today(data.zone);
  const weekday = (new Date(`${now}T12:00:00Z`).getUTCDay() + 6) % 7;
  const monday = addDays(now, -weekday);
  const [bookings, services, hours, payments] = await Promise.all([
    owner(`/businesses/${data.slug}/bookings?start=${monday}&days=14`),
    owner(`/businesses/${data.slug}/services`),
    owner(`/businesses/${data.slug}/hours`),
    owner(`/businesses/${data.slug}/payments`),
  ]);
  if (bookings.status === 200 && bookings.data && Array.isArray(bookings.data.bookings)) {
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
  notice("app-msg", "");
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

// Owner Bookings screen: bookings by day in two-week pages, look-up by reference, details and cancelling.
// All text reaches the page through textContent.

import { addDays, dateParts, el, fill, money, today } from "./gn.js";
import { bookingChip, notice, owner, publicBusiness, startOwnerPage } from "./owner.js";

const RANGE_DAYS = 14;
const REFERENCE = /^[A-Za-z0-9]{10}$/;
/** An email made only of ordinary characters. Anything else is shown as text, never as a link, because a "?" or "&" could pre-fill a message when clicked. */
const PLAIN_EMAIL = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
const input = (id) => document.getElementById(id);
const data = { slug: "", currency: "GBP", zone: "Europe/London", start: "", bookings: [], filter: "all", open: "" };

/**
 * Format an instant in the business's time zone.
 * @param {string} iso An ISO 8601 instant.
 * @returns {string} For example "Mon 26 Oct, 14:15".
 */
function when(iso) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: data.zone,
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

/**
 * The details of one booking, with a cancel step for a future confirmed booking.
 * @param {object} b The booking.
 * @returns {HTMLElement} The details.
 */
function details(b) {
  const rows = [
    ["Service", b.service_name],
    ["When", `${dateParts(b.date).long}, ${b.label}`],
    ["Price", b.price_minor ? money(b.price_minor, data.currency) : "Free"],
    ["Email", b.customer_email],
    ["Phone", b.customer_phone || "Not given"],
    ["Note", b.customer_note || "None"],
    ["Booked", when(b.created_at)],
    ["Reference", b.reference],
  ];
  const box = el(
    "div",
    { class: "bk-details" },
    el(
      "dl",
      {},
      ...rows.flatMap(([label, value]) => {
        const dd =
          label === "Email" && PLAIN_EMAIL.test(value)
            ? el("dd", {}, el("a", { href: `mailto:${value}` }, value))
            : el("dd", {}, String(value));
        return [el("dt", {}, label), dd];
      }),
    ),
  );
  const future = new Date(b.starts_at) > Date.now();
  if (b.status === "confirmed" && future) {
    const message = el("p", { class: "small muted", role: "status" });
    const confirm = el("button", { type: "button", class: "btn small" }, "Yes, cancel it");
    const keep = el("button", { type: "button", class: "btn ghost small" }, "Keep it");
    const ask = el(
      "div",
      { class: "confirm", hidden: true },
      el("p", { class: "small" }, `Cancel ${b.customer_name}'s booking? They'll get an email saying it's cancelled.`),
      el("div", { class: "row" }, confirm, keep),
    );
    const start = el("button", { type: "button", class: "chip-btn" }, "Cancel this booking");
    start.addEventListener("click", () => {
      ask.hidden = false;
      start.hidden = true;
    });
    keep.addEventListener("click", () => {
      ask.hidden = true;
      start.hidden = false;
    });
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      confirm.textContent = "Cancelling…";
      const result = await owner(`/businesses/${data.slug}/bookings/${b.reference}/cancel`, { method: "POST" });
      if (result.status === 200) {
        notice("app-msg", `Cancelled ${b.reference}. The customer has been emailed.`, true);
        data.open = b.reference;
        await load();
        return;
      }
      confirm.disabled = false;
      confirm.textContent = "Yes, cancel it";
      message.textContent = "The booking couldn't be cancelled right now. Please try again.";
    });
    box.append(start, ask, message);
  }
  return box;
}

/**
 * One booking row that opens its details.
 * @param {object} b The booking.
 * @returns {HTMLElement} The row and its details.
 */
function row(b) {
  const open = data.open === b.reference;
  const wrap = el("div", { class: "bk-item" });
  const button = el(
    "button",
    { type: "button", class: "bk-row as-button", "aria-expanded": String(open) },
    el("span", { class: "bk-time" }, b.time),
    el("span", { class: "who" }, el("strong", {}, b.customer_name), el("span", {}, b.service_name)),
    el("span", { class: "meta" }, bookingChip(b, data.currency), el("span", { class: "ref" }, b.reference)),
  );
  button.addEventListener("click", () => {
    data.open = open ? "" : b.reference;
    showList();
  });
  wrap.append(button);
  if (open) wrap.append(details(b));
  return wrap;
}

/** Draw the list for the current page and filter. */
function showList() {
  const last = addDays(data.start, RANGE_DAYS - 1);
  const a = dateParts(data.start);
  const z = dateParts(last);
  input("range-label").textContent = `${a.day} ${a.month} – ${z.day} ${z.month}`;
  for (const [id, key] of [
    ["tab-all", "all"],
    ["tab-live", "live"],
    ["tab-cancelled", "cancelled"],
  ]) {
    input(id).setAttribute("aria-selected", String(data.filter === key));
  }
  const rows = data.bookings.filter((b) =>
    data.filter === "all"
      ? b.status !== "expired"
      : data.filter === "live"
        ? b.status === "confirmed" || b.status === "pending_payment"
        : b.status === "cancelled",
  );
  if (!rows.length) {
    fill(input("bk-list"), el("p", { class: "pad muted" }, "No bookings to show for these two weeks."));
    return;
  }
  const parts = [];
  let current = "";
  for (const b of rows) {
    if (b.date !== current) {
      current = b.date;
      const p = dateParts(b.date);
      parts.push(el("div", { class: "day-label" }, `${p.dow} ${p.day} ${p.month}`));
    }
    parts.push(row(b));
  }
  fill(input("bk-list"), ...parts);
}

/** Load the current two weeks. */
async function load() {
  fill(input("bk-list"), el("p", { class: "loading pad" }, "Loading bookings…"));
  const result = await owner(`/businesses/${data.slug}/bookings?start=${data.start}&days=${RANGE_DAYS}`);
  if (result.status !== 200 || !result.data || !Array.isArray(result.data.bookings)) {
    fill(input("bk-list"), el("p", { class: "pad muted" }, "Bookings couldn't load. Try again in a moment."));
    return;
  }
  data.zone = result.data.time_zone || data.zone;
  data.bookings = result.data.bookings;
  showList();
}

/**
 * Look a booking up by reference and show it.
 * @param {string} reference The reference.
 */
async function lookUp(reference) {
  const wanted = reference.trim().toUpperCase();
  if (!REFERENCE.test(wanted)) {
    fill(input("look-result"), el("p", { class: "error" }, "A reference has 10 letters and numbers, like H49FV82SPR."));
    return;
  }
  fill(input("look-result"), el("p", { class: "loading" }, "Looking…"));
  const result = await owner(`/businesses/${data.slug}/bookings/${wanted}`);
  if (result.status === 200 && result.data) {
    const b = result.data;
    data.open = b.reference;
    fill(
      input("look-result"),
      el(
        "div",
        { class: "found" },
        el(
          "div",
          { class: "found-head" },
          el("strong", {}, b.customer_name),
          bookingChip(b, data.currency),
          el("span", { class: "ref" }, b.reference),
        ),
        details(b),
      ),
    );
    return;
  }
  fill(
    input("look-result"),
    el(
      "p",
      { class: "error" },
      result.status === 404 ? "No booking with that reference." : "The booking couldn't be loaded. Try again.",
    ),
  );
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("bookings");
  if (!page) return;
  data.slug = page.slug;
  const pub = await publicBusiness(data.slug);
  if (pub) {
    data.currency = pub.currency || data.currency;
    data.zone = pub.time_zone || data.zone;
  }
  data.start = today(data.zone);
  input("prev").addEventListener("click", () => {
    data.start = addDays(data.start, -RANGE_DAYS);
    load();
  });
  input("next").addEventListener("click", () => {
    data.start = addDays(data.start, RANGE_DAYS);
    load();
  });
  for (const [id, key] of [
    ["tab-all", "all"],
    ["tab-live", "live"],
    ["tab-cancelled", "cancelled"],
  ]) {
    input(id).addEventListener("click", () => {
      data.filter = key;
      showList();
    });
  }
  input("look-form").addEventListener("submit", (event) => {
    event.preventDefault();
    lookUp(input("look-ref").value);
  });
  const fromLink = location.hash.slice(1);
  if (REFERENCE.test(fromLink)) {
    input("look-ref").value = fromLink;
    lookUp(fromLink);
  }
  await load();
}

start();

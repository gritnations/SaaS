// Owner Bookings screen: bookings by day in two-week pages, searching all bookings, details and cancelling.
// All text reaches the page through textContent.

import { addDays, dateParts, el, fill, money, today } from "./gn.js";
import { bookingChip, notice, owner, publicBusiness, startOwnerPage } from "./owner.js";

const RANGE_DAYS = 14;
const REFERENCE = /^[A-Za-z0-9]{10}$/;
/** An email made only of ordinary characters. Anything else is shown as text, never as a link, because a "?" or "&" could pre-fill a message when clicked. */
const PLAIN_EMAIL = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
const input = (id) => document.getElementById(id);
const data = { slug: "", currency: "GBP", zone: "Europe/London", start: "", bookings: [], filter: "all", open: "" };
/** The search on screen. `asked` numbers each request, so that a slow answer cannot replace a newer one. */
const search = { query: "", bookings: [], understood: [], more: false, open: "", asked: 0 };

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
      el(
        "p",
        { class: "small" },
        `Cancel ${b.customer_name}'s booking? They'll get an email saying it's cancelled.` +
          (b.payment_status === "paid" ? " Their card payment will be refunded in full." : ""),
      ),
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
        await Promise.all([load(), search.query ? runSearch(search.query) : null]);
        return;
      }
      confirm.disabled = false;
      confirm.textContent = "Yes, cancel it";
      message.textContent =
        result.data && typeof result.data.detail === "string"
          ? result.data.detail
          : "The booking couldn't be cancelled right now. Please try again.";
    });
    box.append(start, ask, message);
  }
  return box;
}

/**
 * One booking row that opens its details.
 * @param {object} b The booking.
 * @param {{open: string}} view The list the row belongs to, which remembers the open booking.
 * @param {() => void} redraw Draws that list again.
 * @returns {HTMLElement} The row and its details.
 */
function row(b, view, redraw) {
  const open = view.open === b.reference;
  const wrap = el("div", { class: "bk-item" });
  const button = el(
    "button",
    { type: "button", class: "bk-row as-button", "aria-expanded": String(open), "data-ref": b.reference },
    el("span", { class: "bk-time" }, b.time),
    el("span", { class: "who" }, el("strong", {}, b.customer_name), el("span", {}, b.service_name)),
    el("span", { class: "meta" }, bookingChip(b, data.currency), el("span", { class: "ref" }, b.reference)),
  );
  button.addEventListener("click", () => {
    view.open = open ? "" : b.reference;
    redraw();
    // The list is rebuilt, so put keyboard focus back on the same booking.
    const again = [...document.querySelectorAll(`[data-ref="${b.reference}"]`)];
    const inSearch = view === search;
    again.find((found) => input("search-result").contains(found) === inSearch)?.focus();
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
  fill(input("bk-list"), ...dayRows(rows, data, showList));
}

/**
 * Booking rows with a heading for each day.
 * @param {object[]} bookings The bookings, in the order to show them.
 * @param {{open: string}} view The list they belong to.
 * @param {() => void} redraw Draws that list again.
 * @param {boolean} [withYear] Whether the headings name the year.
 * @returns {HTMLElement[]} Headings and rows.
 */
function dayRows(bookings, view, redraw, withYear = false) {
  const parts = [];
  let current = "";
  for (const b of bookings) {
    if (b.date !== current) {
      current = b.date;
      const p = dateParts(b.date);
      const year = withYear ? ` ${b.date.slice(0, 4)}` : "";
      parts.push(el("div", { class: "day-label" }, `${p.dow} ${p.day} ${p.month}${year}`));
    }
    parts.push(row(b, view, redraw));
  }
  return parts;
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

/** Draw what the last search found. */
function showSearch() {
  const count = search.bookings.length;
  input("search-status").textContent = search.more
    ? `Showing the first ${count}. Add a name, service or date to narrow it.`
    : count === 0
      ? "No bookings match."
      : `${count} ${count === 1 ? "booking" : "bookings"} found.`;
  const read = search.understood.length ? search.understood : ["All bookings"];
  const parts = [
    el("ul", { class: "understood", "aria-label": "Searched for" }, ...read.map((line) => el("li", {}, line))),
  ];
  if (count) parts.push(el("div", { class: "found" }, ...dayRows(search.bookings, search, showSearch, true)));
  fill(input("search-result"), ...parts);
}

/** Take the search off the screen. Any answer still on its way is ignored. */
function clearSearch() {
  search.asked += 1;
  Object.assign(search, { query: "", bookings: [], understood: [], more: false, open: "" });
  input("search-status").textContent = "";
  fill(input("search-result"));
  input("search-clear").hidden = true;
}

/**
 * Search all bookings and show the answer. The text travels in the request body, never in an address.
 * @param {string} typed What the owner typed.
 */
async function runSearch(typed) {
  const query = typed.trim();
  if (!query) {
    clearSearch();
    return;
  }
  const asked = (search.asked += 1);
  // An earlier search's results must not sit under new text while the answer is on its way.
  if (query !== search.query) fill(input("search-result"));
  search.query = query;
  input("search-clear").hidden = false;
  input("search-status").textContent = "Searching…";
  const result = await owner(`/businesses/${data.slug}/bookings/search`, { method: "POST", body: { query } });
  if (asked !== search.asked) return;
  if (result.status !== 200 || !result.data || !Array.isArray(result.data.bookings)) {
    search.bookings = [];
    fill(input("search-result"));
    input("search-status").textContent = "The search couldn't run right now. Please try again.";
    return;
  }
  data.zone = result.data.time_zone || data.zone;
  search.bookings = result.data.bookings;
  search.understood = result.data.understood || [];
  search.more = Boolean(result.data.more);
  // A single result opens by itself, and a booking already open stays open.
  if (search.bookings.length === 1) search.open = search.bookings[0].reference;
  showSearch();
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
  input("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    search.open = "";
    runSearch(input("search-text").value);
  });
  input("search-clear").addEventListener("click", () => {
    input("search-text").value = "";
    clearSearch();
    input("search-text").focus();
  });
  const fromLink = location.hash.slice(1);
  if (REFERENCE.test(fromLink)) {
    input("search-text").value = fromLink;
    runSearch(fromLink);
  }
  await load();
}

start();

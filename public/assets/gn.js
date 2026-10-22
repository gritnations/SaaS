// Shared helpers for the customer pages. All text reaches the page through textContent, never as HTML.

/**
 * Call the platform API on the same site.
 * Every non-GET request sends a JSON body, because the host refuses a body-less POST.
 * @param {string} path The API path, starting with "/".
 * @param {{method?: string, body?: object}} [options] The method and JSON body.
 * @returns {Promise<{ok: boolean, status: number, data: any}>} The outcome. status 0 means no answer.
 */
export async function api(path, options = {}) {
  const method = options.method || "GET";
  const init = { method, headers: {} };
  if (method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body || {});
  }
  try {
    const response = await fetch(path, init);
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

/**
 * Build an element with attributes and children. Strings become text nodes.
 * @param {string} tag The tag name.
 * @param {Record<string, any>} [attrs] Attributes; "class" sets className, "on*" functions add listeners.
 * @param {...(Node|string|null|undefined|false)} children Children to append.
 * @returns {HTMLElement} The element.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on") && typeof value === "function") node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/**
 * Replace an element's children.
 * @param {Element} node The element.
 * @param {...(Node|string|null|undefined|false)} children The new children.
 */
export function fill(node, ...children) {
  node.replaceChildren(...children.filter((c) => c !== null && c !== undefined && c !== false));
}

/**
 * Format a price in minor units.
 * @param {number} minor Amount in minor units, such as pence.
 * @param {string} currency ISO 4217 code.
 * @returns {string} For example "£12" or "£12.50", or "Free" for zero.
 */
export function money(minor, currency) {
  if (!minor) return "Free";
  const whole = minor % 100 === 0;
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    minimumFractionDigits: whole ? 0 : 2,
  }).format(minor / 100);
}

/**
 * Split a local date into parts for display.
 * @param {string} iso A date as "YYYY-MM-DD".
 * @returns {{dow: string, day: string, month: string, long: string}} Weekday, day, month and a long form.
 */
export function dateParts(iso) {
  const when = new Date(`${iso}T12:00:00Z`);
  const part = (options) => new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", ...options }).format(when);
  return {
    dow: part({ weekday: "short" }),
    day: part({ day: "numeric" }),
    month: part({ month: "short" }),
    long: part({ weekday: "long", day: "numeric", month: "long" }),
  };
}

/**
 * Add days to a local date.
 * @param {string} iso A date as "YYYY-MM-DD".
 * @param {number} days Days to add.
 * @returns {string} The new date as "YYYY-MM-DD".
 */
export function addDays(iso, days) {
  const when = new Date(`${iso}T12:00:00Z`);
  when.setUTCDate(when.getUTCDate() + days);
  return when.toISOString().slice(0, 10);
}

/**
 * Today's date in a time zone.
 * @param {string} timeZone An IANA time zone.
 * @returns {string} The date as "YYYY-MM-DD".
 */
export function today(timeZone) {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(),
  );
}

/**
 * Escape text for a calendar file.
 * @param {string} value The text.
 * @returns {string} The escaped text.
 */
function icsText(value) {
  return String(value)
    .replace(/[\\;,]/g, (c) => `\\${c}`)
    .replace(/\r?\n/g, "\\n");
}

/**
 * Format an instant for a calendar file, in UTC.
 * @param {Date} when The instant.
 * @returns {string} For example "20261023T093000Z".
 */
function icsTime(when) {
  return when
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}/, "");
}

/**
 * Offer a calendar file for one booking.
 * @param {{reference: string, title: string, startsAt: string, minutes: number, location?: string}} event
 *   The booking: reference, title, start (ISO 8601 with offset), length and optional location.
 */
export function downloadCalendar(event) {
  const start = new Date(event.startsAt);
  const end = new Date(start.getTime() + event.minutes * 60000);
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//GritNations//Booking//EN",
    "BEGIN:VEVENT",
    `UID:${icsText(event.reference)}@booking`,
    `DTSTAMP:${icsTime(new Date())}`,
    `DTSTART:${icsTime(start)}`,
    `DTEND:${icsTime(end)}`,
    `SUMMARY:${icsText(event.title)}`,
    event.location ? `LOCATION:${icsText(event.location)}` : null,
    "END:VEVENT",
    "END:VCALENDAR",
  ].filter(Boolean);
  const blob = new Blob([lines.join("\r\n") + "\r\n"], { type: "text/calendar" });
  const link = el("a", { href: URL.createObjectURL(blob), download: `booking-${event.reference}.ics` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

/**
 * Build the business's contact block, or null when there is nothing to show.
 * @param {{name: string, address?: string|null, phone?: string|null, email?: string|null}} business The business.
 * @returns {HTMLElement|null} The block.
 */
export function contactBlock(business) {
  const rows = [];
  if (business.address) {
    const maps = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(business.address)}`;
    rows.push(el("span", {}, business.address));
    rows.push(el("a", { href: maps, target: "_blank", rel: "noopener noreferrer" }, "Open in Maps →"));
  }
  if (business.phone) rows.push(el("a", { href: `tel:${business.phone.replace(/[^0-9+]/g, "")}` }, business.phone));
  if (business.email) rows.push(el("a", { href: `mailto:${business.email}` }, business.email));
  if (!rows.length) return null;
  return el(
    "section",
    { class: "panel contact", id: "find-us", "aria-labelledby": "find-us-h" },
    el("span", { class: "eyebrow", id: "find-us-h" }, `Contact ${business.name}`),
    ...rows,
  );
}

/**
 * Show the business in the page header and title.
 * @param {string} name The business name.
 * @param {string} title The document title.
 */
export function showBusiness(name, title) {
  document.title = title;
  const badge = document.getElementById("badge");
  const heading = document.getElementById("business-name");
  if (badge) badge.textContent = (name.trim()[0] || "?").toUpperCase();
  if (heading) heading.textContent = name;
}

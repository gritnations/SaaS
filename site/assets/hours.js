// Owner Opening hours screen: a weekly editor with one or more time blocks per day.
// All text reaches the page through textContent.

import { el, fill } from "./gn.js";
import { notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
/** Display order Monday first; the API numbers days 0 = Sunday to 6 = Saturday. */
const DAYS = [
  [1, "Monday"],
  [2, "Tuesday"],
  [3, "Wednesday"],
  [4, "Thursday"],
  [5, "Friday"],
  [6, "Saturday"],
  [0, "Sunday"],
];
let slug = "";
/** Blocks per weekday: { 0: [{start, end}], ... }. */
const week = {};

/**
 * Turn "HH:MM" into minutes after midnight.
 * @param {string} value The time.
 * @returns {number} The minutes, or NaN.
 */
function toMinutes(value) {
  const match = /^(\d{2}):(\d{2})$/.exec(value || "");
  return match ? Number(match[1]) * 60 + Number(match[2]) : NaN;
}

/**
 * One time block with start, end and a remove button.
 * @param {number} day The weekday.
 * @param {number} index The block's position.
 * @returns {HTMLElement} The block.
 */
function blockRow(day, index) {
  const block = week[day][index];
  const start = el("input", { type: "time", step: 300, "aria-label": "Opens at" });
  start.value = block.start;
  const end = el("input", { type: "time", step: 300, "aria-label": "Closes at" });
  end.value = block.end;
  start.addEventListener("change", () => {
    block.start = start.value;
  });
  end.addEventListener("change", () => {
    block.end = end.value;
  });
  const remove = el("button", { type: "button", class: "mini", "aria-label": "Remove this time block" }, "Remove");
  remove.addEventListener("click", () => {
    week[day].splice(index, 1);
    draw();
  });
  return el("div", { class: "block" }, start, el("span", { "aria-hidden": "true" }, "–"), end, remove);
}

/** Draw the week. */
function draw() {
  fill(
    input("hours"),
    ...DAYS.map(([day, name]) => {
      const open = week[day].length > 0;
      const toggle = el("input", { type: "checkbox", id: `day-${day}` });
      toggle.checked = open;
      toggle.addEventListener("change", () => {
        week[day] = toggle.checked ? [{ start: "09:00", end: "17:00" }] : [];
        draw();
      });
      const add = el("button", { type: "button", class: "chip-btn" }, "Add a time block");
      add.addEventListener("click", () => {
        const last = week[day][week[day].length - 1];
        week[day].push({ start: last ? last.end : "09:00", end: "17:00" });
        draw();
      });
      return el(
        "div",
        { class: open ? "day-row" : "day-row closed" },
        el("label", { class: "check-row compact", for: `day-${day}` }, toggle, el("strong", {}, name)),
        open
          ? el("div", { class: "blocks" }, ...week[day].map((_, i) => blockRow(day, i)), add)
          : el("span", { class: "muted small" }, "Closed"),
      );
    }),
  );
}

/**
 * Check the week before saving.
 * @returns {string} A problem to show, or "" when the week is fine.
 */
function problem() {
  for (const [day, name] of DAYS) {
    const blocks = week[day]
      .map((b) => ({ start: toMinutes(b.start), end: toMinutes(b.end) }))
      .sort((a, b) => a.start - b.start);
    for (let i = 0; i < blocks.length; i += 1) {
      const b = blocks[i];
      if (Number.isNaN(b.start) || Number.isNaN(b.end)) return `${name}: enter both times.`;
      if (b.end <= b.start) return `${name}: each block must end after it starts.`;
      if (i > 0 && b.start < blocks[i - 1].end) return `${name}: time blocks overlap.`;
    }
  }
  return "";
}

/** Save the week. */
async function save() {
  const issue = problem();
  if (issue) {
    notice("hours-msg", issue);
    return;
  }
  const blocks = DAYS.flatMap(([day]) => week[day].map((b) => ({ weekday: day, start: b.start, end: b.end })));
  input("save-hours").disabled = true;
  const result = await owner(`/businesses/${slug}/hours`, { method: "PUT", body: { blocks } });
  input("save-hours").disabled = false;
  notice(
    "hours-msg",
    result.status === 200
      ? "Opening hours saved. Your booking page uses them now."
      : "The hours couldn't be saved. Check each time and try again.",
    result.status === 200,
  );
}

/** The business's own time zone, used for "today" and nothing else: days off are plain calendar dates. */
let zone = "Europe/London";

/**
 * Show a calendar date such as "Mon 16 Nov 2026". It is a plain date, so no time zone may shift it.
 * @param {string} iso The date, "YYYY-MM-DD".
 * @returns {string} The date for display.
 */
function dayLabel(iso) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date(`${iso}T00:00:00Z`));
}

/**
 * Describe one days-off entry.
 * @param {{first_day: string, last_day: string}} entry The entry.
 * @returns {string} One date, or "first to last".
 */
function rangeLabel(entry) {
  return entry.first_day === entry.last_day
    ? dayLabel(entry.first_day)
    : `${dayLabel(entry.first_day)} to ${dayLabel(entry.last_day)}`;
}

/** @returns {string} Today's date in the business's own calendar, "YYYY-MM-DD". */
function localToday() {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: zone }).format(new Date());
}

/** Keep the date pickers from offering a past day, or an end before the start. */
function limitDates() {
  const today = localToday();
  input("off-from").min = today;
  input("off-to").min = input("off-from").value || today;
}

/**
 * Draw the list of days off.
 * @param {Array<{id: string, first_day: string, last_day: string, note: string|null}>} entries The entries.
 */
function drawOff(entries) {
  const list = input("off-list");
  if (!entries.length) {
    fill(list, el("li", { class: "muted small" }, "No days off planned."));
    return;
  }
  fill(
    list,
    ...entries.map((entry) =>
      el(
        "li",
        { class: "off-item" },
        el(
          "div",
          {},
          el("p", {}, el("strong", {}, rangeLabel(entry))),
          entry.note ? el("p", { class: "small muted" }, entry.note) : null,
        ),
        el(
          "button",
          {
            class: "btn ghost small",
            type: "button",
            "aria-label": `Remove days off ${rangeLabel(entry)}`,
            onclick: () => removeOff(entry.id),
          },
          "Remove",
        ),
      ),
    ),
  );
}

/**
 * Read the reason the server gave, if it gave one in words.
 * @param {{data: object|null}} result The answer from the owner API.
 * @returns {string} The reason, or an empty string.
 */
function reason(result) {
  return result.data && typeof result.data.detail === "string" ? result.data.detail : "";
}

/**
 * Add the days off in the form.
 * @param {SubmitEvent} event The form submission.
 */
async function addOff(event) {
  event.preventDefault();
  const first = input("off-from").value;
  const last = input("off-to").value;
  if (!first) {
    notice("off-msg", "Choose the first day.");
    input("off-from").focus();
    return;
  }
  if (last && last < first) {
    notice("off-msg", "The last day can't be before the first day.");
    input("off-to").focus();
    return;
  }
  const button = input("off-add");
  button.disabled = true;
  const body = { first_day: first, note: input("off-note").value.trim() };
  if (last) body.last_day = last;
  const result = await owner(`/businesses/${slug}/time-off`, { method: "POST", body });
  button.disabled = false;
  if (result.status === 201 && result.data && Array.isArray(result.data.entries)) {
    drawOff(result.data.entries);
    input("off-form").reset();
    limitDates();
    const count = Number(result.data.bookings_affected) || 0;
    if (count > 0) {
      const one = count === 1;
      notice(
        "off-msg",
        `Days off added. ${count} ${one ? "booking already falls" : "bookings already fall"} in these days. ` +
          `${one ? "It hasn't" : "They haven't"} been changed.`,
        true,
      );
      input("off-msg").append(" ", el("a", { href: "/app/bookings/" }, "Review in Bookings"));
    } else {
      notice("off-msg", "Days off added. Customers won't see times on those days.", true);
    }
    return;
  }
  notice("off-msg", reason(result) || "The days off couldn't be saved. Check the dates and try again.");
}

/**
 * Remove one days-off entry, which opens those days again.
 * @param {string} id The entry's identifier.
 */
async function removeOff(id) {
  const result = await owner(`/businesses/${slug}/time-off/${id}/remove`, { method: "POST" });
  if (result.status === 200 && result.data && Array.isArray(result.data.entries)) {
    drawOff(result.data.entries);
    notice("off-msg", "Days off removed. Those days are open again.", true);
    return;
  }
  notice("off-msg", reason(result) || "The days off couldn't be removed. Please try again.");
}

/**
 * Start the Days off panel.
 * @param {string} timeZone The business's IANA time zone.
 */
async function startDaysOff(timeZone) {
  try {
    new Intl.DateTimeFormat("sv-SE", { timeZone });
    zone = timeZone || "Europe/London";
  } catch {
    zone = "Europe/London";
  }
  limitDates();
  input("off-from").addEventListener("input", limitDates);
  input("off-form").addEventListener("submit", addOff);
  const result = await owner(`/businesses/${slug}/time-off`);
  if (result.status === 200 && result.data && Array.isArray(result.data.entries)) drawOff(result.data.entries);
  else
    fill(
      input("off-list"),
      el("li", { class: "muted small" }, "Days off couldn't load. Refresh the page to try again."),
    );
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("hours");
  if (!page) return;
  slug = page.slug;
  startDaysOff(page.business.time_zone);
  for (const [day] of DAYS) week[day] = [];
  const result = await owner(`/businesses/${slug}/hours`);
  if (result.status !== 200 || !result.data || !Array.isArray(result.data.blocks)) {
    fill(input("hours"), el("p", { class: "muted" }, "Opening hours couldn't load. Refresh the page to try again."));
    input("save-hours").hidden = true;
    return;
  }
  for (const b of result.data.blocks) week[b.weekday].push({ start: b.start.slice(0, 5), end: b.end.slice(0, 5) });
  draw();
  input("save-hours").addEventListener("click", save);
}

start();

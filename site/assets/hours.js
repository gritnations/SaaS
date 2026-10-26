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

/** Start the page. */
async function start() {
  const page = await startOwnerPage("hours");
  if (!page) return;
  slug = page.slug;
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

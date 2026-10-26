// Owner Settings screen: business name, contact details, meeting link and booking rules.
// All text reaches the page through textContent.

import { notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
let slug = "";

/**
 * Read a number of hours as minutes.
 * @param {string} id The field id.
 * @returns {number} The minutes, or NaN.
 */
function hoursToMinutes(id) {
  const value = Number(input(id).value);
  return Number.isFinite(value) ? Math.round(value * 60) : NaN;
}

/**
 * Show minutes as hours, without trailing zeros.
 * @param {number} minutes The minutes.
 * @returns {string} The hours.
 */
function minutesToHours(minutes) {
  return String(Math.round((minutes / 60) * 100) / 100);
}

/**
 * Fill the form from the saved settings.
 * @param {object} s The settings.
 */
function fillForm(s) {
  input("s-name").value = s.name || "";
  input("s-contact").value = s.contact_email || "";
  input("s-meeting").value = s.meeting_link || "";
  input("s-address").value = s.public_address || "";
  input("s-phone").value = s.public_phone || "";
  input("s-email").value = s.public_email || "";
  input("s-notice").value = minutesToHours(s.notice_minutes);
  input("s-window").value = String(s.window_days);
  input("s-break").value = String(s.break_minutes);
  input("s-cancel").value = minutesToHours(s.cancel_cutoff_minutes);
  input("s-move").value = minutesToHours(s.move_cutoff_minutes);
}

/**
 * Save the form.
 * @param {SubmitEvent} event The submit event.
 */
async function save(event) {
  event.preventDefault();
  const body = {
    name: input("s-name").value.trim(),
    contact_email: input("s-contact").value.trim(),
    meeting_link: input("s-meeting").value.trim(),
    public_address: input("s-address").value.trim(),
    public_phone: input("s-phone").value.trim(),
    public_email: input("s-email").value.trim(),
    notice_minutes: hoursToMinutes("s-notice"),
    window_days: Number(input("s-window").value),
    break_minutes: Number(input("s-break").value),
    cancel_cutoff_minutes: hoursToMinutes("s-cancel"),
    move_cutoff_minutes: hoursToMinutes("s-move"),
  };
  if (!body.name) return notice("settings-msg", "Enter your business name.");
  if (body.meeting_link && !body.meeting_link.startsWith("https://")) {
    return notice("settings-msg", "The meeting link must start with https://.");
  }
  const numbers = ["notice_minutes", "window_days", "break_minutes", "cancel_cutoff_minutes", "move_cutoff_minutes"];
  if (numbers.some((key) => !Number.isFinite(body[key]) || body[key] < 0)) {
    return notice("settings-msg", "Fill in every booking rule with a number.");
  }
  input("save-settings").disabled = true;
  const result = await owner(`/businesses/${slug}/settings`, { method: "PATCH", body });
  input("save-settings").disabled = false;
  if (result.status === 200 && result.data) {
    fillForm(result.data);
    notice("settings-msg", "Settings saved. Your booking page uses them now.", true);
    return;
  }
  notice(
    "settings-msg",
    result.status === 422
      ? "Check the values: hours 0–720, days 1–365, a break of 0–240 minutes, valid emails, and an address of at least 3 characters."
      : "Your settings couldn't be saved right now. Please try again.",
  );
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("settings");
  if (!page) return;
  slug = page.slug;
  const result = await owner(`/businesses/${slug}/settings`);
  if (result.status !== 200 || !result.data) {
    notice("settings-msg", "Your settings couldn't load. Refresh the page to try again.");
    input("save-settings").disabled = true;
    return;
  }
  fillForm(result.data);
  input("settings-form").addEventListener("submit", save);
}

start();

// Owner Services screen: edit each service in place and add new ones. All text reaches the page through textContent.

import { el, fill } from "./gn.js";
import { notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
let slug = "";

/**
 * Make a service's public identifier from its name.
 * @param {string} name The service name.
 * @returns {string} Lower-case letters, digits and hyphens, or "" when the name has too few of them.
 */
function slugFrom(name) {
  const base = name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/g, "");
  return base.length >= 2 ? base : "";
}

/**
 * Read a price in pounds as minor units.
 * @param {string} text The typed price.
 * @returns {number|null} The price in pence, or null when it is not a valid price.
 */
function pence(text) {
  const value = Number(String(text).replace(/[£,\s]/g, "") || "0");
  if (!Number.isFinite(value) || value < 0 || value > 100000) return null;
  return Math.round(value * 100);
}

/**
 * Check a length in minutes.
 * @param {string} text The typed length.
 * @returns {number|null} The minutes, or null when outside 5 to 480.
 */
function minutes(text) {
  const value = Number(text);
  return Number.isInteger(value) && value >= 5 && value <= 480 ? value : null;
}

/**
 * One editable service card.
 * @param {object} s The service.
 * @returns {HTMLElement} The card.
 */
function card(s) {
  const id = `svc-${s.slug}`;
  const name = el("input", { id: `${id}-name`, type: "text", maxlength: 120 });
  name.value = s.name;
  const mins = el("input", { id: `${id}-min`, type: "number", min: 5, max: 480, step: 5 });
  mins.value = String(s.duration_minutes);
  const price = el("input", { id: `${id}-price`, type: "number", min: 0, step: "0.01" });
  price.value = (s.price_minor / 100).toFixed(2);
  const active = el("input", { id: `${id}-active`, type: "checkbox" });
  active.checked = s.active;
  const message = el("span", { class: "small", role: "status" });
  const save = el("button", { type: "button", class: "btn small" }, "Save");
  save.addEventListener("click", async () => {
    const body = { name: name.value.trim(), duration_minutes: minutes(mins.value), price_minor: pence(price.value) };
    if (!body.name || body.duration_minutes === null || body.price_minor === null) {
      message.textContent = "Check the name, a length of 5 to 480 minutes, and the price.";
      message.className = "small error";
      return;
    }
    body.active = active.checked;
    save.disabled = true;
    const result = await owner(`/businesses/${slug}/services/${s.slug}`, { method: "PATCH", body });
    save.disabled = false;
    message.className = result.status === 200 ? "small ok" : "small error";
    message.textContent = result.status === 200 ? "Saved ✓" : "It couldn't be saved. Check the values and try again.";
  });
  const box = el(
    "div",
    { class: s.active ? "svc-card" : "svc-card off" },
    el("label", { class: "field", for: `${id}-name` }, "Name", name),
    el("label", { class: "field", for: `${id}-min` }, "Minutes", mins),
    el("label", { class: "field", for: `${id}-price` }, "Price (£)", price),
    el("label", { class: "check-row compact", for: `${id}-active` }, active, el("span", {}, "Shown on booking page")),
    el("div", { class: "row" }, save, message),
  );
  active.addEventListener("change", () => box.classList.toggle("off", !active.checked));
  return box;
}

/** Load and draw the services. */
async function load() {
  const result = await owner(`/businesses/${slug}/services`);
  if (result.status !== 200 || !Array.isArray(result.data)) {
    fill(input("svc-list"), el("p", { class: "muted" }, "Services couldn't load. Refresh the page to try again."));
    return;
  }
  if (!result.data.length) {
    fill(input("svc-list"), el("p", { class: "muted" }, "No services yet. Add your first one below."));
    return;
  }
  fill(input("svc-list"), ...result.data.map(card));
}

/**
 * Add a service from the form.
 * @param {SubmitEvent} event The submit event.
 */
async function add(event) {
  event.preventDefault();
  notice("add-msg", "");
  const name = input("add-name").value.trim();
  const body = {
    slug: slugFrom(name),
    name,
    duration_minutes: minutes(input("add-min").value),
    price_minor: pence(input("add-price").value),
    active: true,
    sort_order: 100,
  };
  if (!body.slug) return notice("add-msg", "Give the service a name with at least two letters or numbers.");
  if (body.duration_minutes === null) return notice("add-msg", "Use a length between 5 and 480 minutes.");
  if (body.price_minor === null) return notice("add-msg", "Enter a price, or 0 for a free service.");
  const result = await owner(`/businesses/${slug}/services`, { method: "POST", body });
  if (result.status === 201) {
    input("add-form").reset();
    input("add-min").value = "30";
    input("add-price").value = "0";
    notice("svc-msg", `Added ${name}. It's on your booking page now.`, true);
    await load();
    return;
  }
  notice(
    "add-msg",
    result.status === 409
      ? "You already have a service with that name."
      : "It couldn't be added. Check the values and try again.",
  );
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("services");
  if (!page) return;
  slug = page.slug;
  input("add-form").addEventListener("submit", add);
  await load();
}

start();

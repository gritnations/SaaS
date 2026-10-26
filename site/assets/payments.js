// Owner Payments screen: connect Stripe for card payments and choose how customers pay.
// All text reaches the page through textContent.

import { notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
let slug = "";
let state = null;

/** Show a message for a return from Stripe's set-up: #payments=done or #payments=retry. */
function readReturn() {
  const match = /^#payments=(done|retry)$/.exec(location.hash);
  if (!match) return;
  history.replaceState(null, "", location.pathname);
  notice(
    "return-msg",
    match[1] === "done"
      ? "Thanks. Stripe is checking your details; card payments switch on as soon as it's done."
      : "Your Stripe set-up isn't finished yet. Press Continue with Stripe to pick up where you left off.",
    match[1] === "done",
  );
}

/** Draw the Stripe card and the options from the current set-up. */
function draw() {
  const s = state;
  const button = input("connect");
  if (!s.available) {
    input("stripe-state").textContent =
      "Card payments aren't available on this platform yet. Customers can pay at the venue.";
    button.hidden = true;
  } else if (s.connected && s.enabled) {
    input("stripe-state").textContent = "Connected. Card payments go straight to your Stripe account.";
    button.hidden = true;
  } else if (s.connected) {
    input("stripe-state").textContent =
      "Started, but Stripe still needs some details before you can take card payments.";
    button.textContent = "Continue with Stripe";
    button.hidden = false;
  } else {
    input("stripe-state").textContent =
      "Connect a Stripe account to let customers pay by card when they book. Stripe handles the card details.";
    button.textContent = "Connect Stripe";
    button.hidden = false;
  }
  const canOnline = Boolean(s.available && s.connected && s.enabled && s.online_allowed);
  input("opt-online").checked = Boolean(s.pay_online);
  input("opt-online").disabled = !canOnline && !s.pay_online;
  input("online-note").textContent = canOnline
    ? "Customers pay when they book."
    : !s.online_allowed
      ? "Not included in your current plan."
      : "Connect Stripe first.";
  input("opt-venue").checked = Boolean(s.pay_at_venue);
}

/** Load the set-up. */
async function load() {
  const result = await owner(`/businesses/${slug}/payments`);
  if (result.status === 503) {
    state = { available: false, pay_at_venue: true };
    draw();
    return;
  }
  if (result.status !== 200 || !result.data) {
    input("stripe-state").textContent = "Your payment set-up couldn't load. Refresh the page to try again.";
    return;
  }
  state = result.data;
  draw();
}

/** Open Stripe's set-up. */
async function connect() {
  const button = input("connect");
  button.disabled = true;
  button.textContent = "Opening Stripe…";
  const result = await owner(`/businesses/${slug}/payments/connect`, { method: "POST" });
  if (result.status === 200 && result.data && /^https:\/\//.test(result.data.url || "")) {
    location.assign(result.data.url);
    return;
  }
  button.disabled = false;
  draw();
  notice("return-msg", "Stripe couldn't be opened just now. Please try again in a minute.");
}

/** Save how customers pay. */
async function saveOptions() {
  const body = { pay_online: input("opt-online").checked, pay_at_venue: input("opt-venue").checked };
  if (!body.pay_online && !body.pay_at_venue) {
    notice("opt-msg", "Choose at least one way for customers to pay for paid services.");
    return;
  }
  input("save-options").disabled = true;
  const result = await owner(`/businesses/${slug}/payments`, { method: "PATCH", body });
  input("save-options").disabled = false;
  if (result.status === 200 && result.data) {
    state = result.data;
    draw();
    notice("opt-msg", "Saved. Your booking page shows these options now.", true);
    return;
  }
  notice("opt-msg", (result.data && result.data.detail) || "Your choice couldn't be saved right now.");
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("payments");
  if (!page) return;
  slug = page.slug;
  readReturn();
  input("connect").addEventListener("click", connect);
  input("save-options").addEventListener("click", saveOptions);
  await load();
}

start();

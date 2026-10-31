// Owner Plan screen: the current plan, upgrades through Stripe Checkout and the Stripe customer portal.
// All text reaches the page through textContent.

import { describePlan, notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
const POLLS = 5;
let slug = "";
let business = null;

/**
 * Draw the current plan.
 * @param {object} b The business from /owner/me.
 */
function draw(b) {
  const plan = describePlan(b);
  input("now-h").textContent = plan.name;
  input("now-note").textContent = plan.note;
  input("manage").hidden = b.plan === "free";
  input("cancel-plan").hidden = b.plan === "free";
  input("plans").hidden = b.plan !== "free";
}

/**
 * Format an amount in minor units as money.
 * @param {number} minor The amount in pence or the currency's minor unit.
 * @param {string} currency The ISO 4217 code.
 * @returns {string} For example "£4.50".
 */
function money(minor, currency) {
  return new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(minor / 100);
}

/**
 * Show the owner what cancelling now would refund, and ask them to confirm.
 */
async function askToCancel() {
  const button = input("cancel-plan");
  button.disabled = true;
  const result = await owner(`/businesses/${slug}/billing/cancel-quote`);
  button.disabled = false;
  const quote = result.data;
  if (result.status !== 200 || !quote || typeof quote.refund_minor !== "number") {
    notice(
      "return-msg",
      (quote && quote.detail) || "We couldn't check your refund right now. Please try again in a minute.",
    );
    return;
  }
  const days = quote.unused_days;
  input("cancel-text").textContent =
    quote.refund_minor > 0
      ? `You'll be refunded ${money(quote.refund_minor, quote.currency)} for the ${days} unused ${days === 1 ? "day" : "days"} of this month. Your plan ends now and you move to the Free plan.`
      : "Your plan ends now and you move to the Free plan. There are no unused days to refund.";
  input("return-msg").hidden = true;
  input("cancel-box").hidden = false;
  input("cancel-confirm").focus();
}

/**
 * Cancel the plan, refund the unused days and show the Free plan.
 */
async function confirmCancel() {
  const button = input("cancel-confirm");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Cancelling…";
  const result = await owner(`/businesses/${slug}/billing/cancel`, { method: "POST", body: {} });
  button.disabled = false;
  button.textContent = label;
  const done = result.data;
  if (result.status === 200 && done && typeof done.refund_minor === "number") {
    business = { ...business, plan: "free", plan_status: "active", plan_ends_at: null };
    draw(business);
    input("cancel-box").hidden = true;
    notice(
      "return-msg",
      done.refund_minor > 0
        ? `Your plan is cancelled. ${money(done.refund_minor, done.currency)} is on its way back to your card. It can take a few days to appear.`
        : "Your plan is cancelled. You're on the Free plan.",
      true,
    );
    return;
  }
  notice(
    "return-msg",
    (done && done.detail) ||
      "Your plan couldn't be cancelled right now. Please try again in a minute. You won't be refunded twice.",
  );
}

/**
 * After a return from Stripe Checkout, wait for the payment notification to change the plan.
 * @param {object} b The business when the page loaded.
 */
async function waitForPlan(b) {
  let current = b;
  for (let i = 0; i < POLLS && current.plan === "free"; i += 1) {
    notice("return-msg", "Payment received. Updating your plan…", true);
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const me = await owner("/me");
    if (me.status === 200 && me.data && Array.isArray(me.data.businesses) && me.data.businesses.length) {
      current = me.data.businesses[0];
    }
  }
  draw(current);
  notice(
    "return-msg",
    current.plan === "free"
      ? "Payment received. Your plan is still updating; refresh this page in a minute."
      : "Payment received. Your plan is active.",
    true,
  );
}

/**
 * Open Stripe.
 * @param {string} path The owner API path that returns a Stripe link.
 * @param {object} body The JSON body.
 * @param {HTMLButtonElement} button The button pressed.
 */
async function openStripe(path, body, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Opening Stripe…";
  const result = await owner(`/businesses/${slug}${path}`, { method: "POST", body });
  if (result.status === 200 && result.data && /^https:\/\//.test(result.data.url || "")) {
    location.assign(result.data.url);
    return;
  }
  button.disabled = false;
  button.textContent = label;
  notice(
    "return-msg",
    (result.data && result.data.detail) || "Stripe couldn't be opened right now. Please try again in a minute.",
  );
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("plan");
  if (!page) return;
  slug = page.slug;
  const b = page.business;
  business = b;
  draw(b);
  for (const button of document.querySelectorAll("[data-plan]")) {
    button.addEventListener("click", () => openStripe("/billing/upgrade", { plan: button.dataset.plan }, button));
  }
  input("manage").addEventListener("click", () => openStripe("/billing/portal", {}, input("manage")));
  input("cancel-plan").addEventListener("click", askToCancel);
  input("cancel-confirm").addEventListener("click", confirmCancel);
  input("cancel-keep").addEventListener("click", () => {
    input("cancel-box").hidden = true;
  });

  const match = /^#billing=(done|cancelled|portal)$/.exec(location.hash);
  if (match) history.replaceState(null, "", location.pathname);
  if (b.plan_status === "past_due") {
    notice("return-msg", "Your last plan payment failed. Use Manage subscription to update your card.");
  } else if (match && match[1] === "done") {
    await waitForPlan(b);
  } else if (match && match[1] === "cancelled") {
    notice("return-msg", "Payment wasn't completed. Your plan hasn't changed.");
  }
}

start();

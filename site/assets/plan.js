// Owner Plan screen: the current plan, upgrades through Stripe Checkout and the Stripe customer portal.
// All text reaches the page through textContent.

import { describePlan, notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);
const POLLS = 5;
let slug = "";

/**
 * Draw the current plan.
 * @param {object} b The business from /owner/me.
 */
function draw(b) {
  const plan = describePlan(b);
  input("now-h").textContent = plan.name;
  input("now-note").textContent = plan.note;
  input("manage").hidden = b.plan === "free";
  input("plans").hidden = b.plan !== "free";
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
  const page = await startOwnerPage("");
  if (!page) return;
  slug = page.slug;
  const b = page.business;
  draw(b);
  for (const button of document.querySelectorAll("[data-plan]")) {
    button.addEventListener("click", () => openStripe("/billing/upgrade", { plan: button.dataset.plan }, button));
  }
  input("manage").addEventListener("click", () => openStripe("/billing/portal", {}, input("manage")));

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

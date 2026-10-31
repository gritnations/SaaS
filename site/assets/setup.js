// Set-up screen after a new owner confirms their email: check the business details and create the business.
// The details typed at sign-up travel with the login, so the form starts filled in. All text reaches the page
// through textContent.

import { regionFromDevice, say, setError } from "./auth.js";
import { owner, readSession, sessionEmail, toSignIn, useSession } from "./owner.js";

const input = (id) => document.getElementById(id);
const fields = ["company", "type", "other", "region", "owner"];

/**
 * Read the sign-up details saved with the login, from the session token's claims.
 * @param {string} token The access token.
 * @returns {object} The details, or an empty object.
 */
function signUpDetails(token) {
  try {
    let part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    while (part.length % 4) part += "=";
    const bytes = Uint8Array.from(atob(part), (c) => c.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes));
    return claims.user_metadata || {};
  } catch {
    return {};
  }
}

/** @returns {boolean} Whether the form can be sent; shows what needs fixing. */
function validate() {
  const type = input("type").value;
  const errors = {
    company: input("company").value.trim() ? "" : "Enter your business name.",
    type: type ? "" : "Choose what your business does.",
    other: type === "other" && !input("other").value.trim() ? "Describe your business in a few words." : "",
    region: input("region").value ? "" : "Choose where your business is.",
    owner: input("owner").value.trim() ? "" : "Enter your name.",
  };
  for (const id of fields) setError(id, errors[id]);
  const first = fields.find((id) => errors[id]);
  if (first) input(first).focus();
  return !first;
}

/**
 * Create the business, then open the dashboard.
 * @param {SubmitEvent} event The submit event.
 */
async function create(event) {
  event.preventDefault();
  say("form-msg", "");
  if (!validate()) return;
  const type = input("type").value;
  const button = input("create");
  button.disabled = true;
  button.textContent = "Setting up…";
  const result = await owner("/businesses", {
    method: "POST",
    body: {
      name: input("company").value.trim(),
      business_type: type,
      business_type_other: type === "other" ? input("other").value.trim() : null,
      owner_name: input("owner").value.trim(),
      plan: "free",
      contact_email: sessionEmail() || null,
      region: input("region").value,
    },
  });
  if (result.status === 201 || result.status === 409) {
    location.replace("/app/");
    return;
  }
  button.disabled = false;
  button.textContent = "Continue to your dashboard";
  say(
    "form-msg",
    result.status === 422
      ? "Please check your details."
      : "Your business couldn't be set up right now. Please try again.",
  );
}

/** Start the page. */
async function start() {
  const session = useSession();
  if (!session || !readSession()) {
    toSignIn();
    return;
  }
  const me = await owner("/me");
  if (me.status === 200 && me.data && Array.isArray(me.data.businesses) && me.data.businesses.length) {
    location.replace("/app/");
    return;
  }
  const meta = signUpDetails(session.access);
  input("company").value = meta.company_name || "";
  input("type").value = meta.business_type || "";
  input("other").value = meta.business_type_other || "";
  input("region").value = ["uk", "ae", "sa", "qa"].includes(meta.region) ? meta.region : regionFromDevice();
  input("owner").value = meta.owner_name || "";
  input("other-wrap").hidden = input("type").value !== "other";
  input("type").addEventListener("change", () => {
    input("other-wrap").hidden = input("type").value !== "other";
  });
  input("setup-form").addEventListener("submit", create);
}

start();

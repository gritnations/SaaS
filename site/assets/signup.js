// Owner sign-up: create the login with the identity provider and send the confirmation email.
// The confirmation link returns to the owner dashboard at the site root, which finishes setting up the
// business from the details saved with the login. All text reaches the page through textContent.

import { AUTH_KEY, AUTH_URL } from "./config.js";

const EMAIL = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$/;
const DASHBOARD = `${location.origin}/`;

const form = document.getElementById("signup-form");
const fields = ["company", "type", "other", "owner", "email", "password", "terms"];
const input = (id) => document.getElementById(id);
let lastEmail = "";
let lastCompany = "";

/**
 * Call the identity provider.
 * @param {string} path The path after /auth/v1, starting with "/".
 * @param {object} body The JSON body.
 * @returns {Promise<{status: number, data: any}>} The answer. status 0 means no answer.
 */
async function auth(path, body) {
  try {
    const response = await fetch(AUTH_URL + path, {
      method: "POST",
      headers: { apikey: AUTH_KEY, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    return { status: response.status, data };
  } catch {
    return { status: 0, data: {} };
  }
}

/**
 * Show or clear one field's error.
 * @param {string} id The field id.
 * @param {string} message The error, or "" to clear it.
 */
function setError(id, message) {
  const error = document.getElementById(`${id}-err`);
  const control = input(id);
  error.textContent = message;
  error.hidden = !message;
  control.setAttribute("aria-invalid", message ? "true" : "false");
  if (message) control.setAttribute("aria-describedby", `${id}-err`);
  else control.removeAttribute("aria-describedby");
}

/**
 * Show a message above the button, or hide it.
 * @param {string} message The message, or "".
 */
function formMessage(message) {
  const box = input("form-msg");
  box.textContent = message;
  box.hidden = !message;
}

/**
 * Check every field and show what needs fixing.
 * @returns {boolean} Whether the form can be sent.
 */
function validate() {
  const company = input("company").value.trim();
  const type = input("type").value;
  const other = input("other").value.trim();
  const errors = {
    company: company ? "" : "Enter your business name. Customers see it on your booking page.",
    type: type ? "" : "Choose what your business does.",
    other: type === "other" && !other ? "Describe your business in a few words." : "",
    owner: input("owner").value.trim() ? "" : "Enter your name.",
    email: EMAIL.test(input("email").value.trim()) ? "" : "Enter an email like name@example.com.",
    password: input("password").value.length >= 8 ? "" : "Your password needs at least 8 characters.",
    terms: input("terms").checked ? "" : "Please accept the terms to create your account.",
  };
  for (const id of fields) setError(id, errors[id]);
  const first = fields.find((id) => errors[id]);
  if (first) input(first).focus();
  return !first;
}

/** Show the password strength bars. */
function showStrength() {
  const length = input("password").value.length;
  const level = length === 0 ? 0 : length < 8 ? 1 : length < 12 ? 2 : 3;
  document.querySelector(".strength").dataset.level = String(level);
}

/**
 * Whether a sign-up answer means the email already has an account.
 * With email confirmation on, the provider answers an existing email with a user that has no identities.
 * @param {{status: number, data: any}} result The answer.
 * @returns {boolean} Whether the account exists.
 */
function alreadyExists(result) {
  const user = result.data.user || result.data;
  const noIdentities = result.status === 200 && user && Array.isArray(user.identities) && user.identities.length === 0;
  return noIdentities || /^(user_already_exists|email_exists)$/.test(result.data.error_code || result.data.code || "");
}

/** Show the "check your email" step. */
function showDone() {
  input("done-email").textContent = lastEmail;
  input("done-company").textContent = lastCompany;
  input("form-view").hidden = true;
  input("done-view").hidden = false;
  input("resend-msg").hidden = true;
  input("done-h").focus();
}

/**
 * Create the login.
 * @param {SubmitEvent} event The submit event.
 */
async function submit(event) {
  event.preventDefault();
  formMessage("");
  if (!validate()) return;
  if (AUTH_KEY.includes("your-key")) {
    formMessage("Sign-up is not configured on this page yet.");
    return;
  }
  const button = input("create");
  button.disabled = true;
  button.textContent = "Creating your account…";
  const type = input("type").value;
  lastEmail = input("email").value.trim();
  lastCompany = input("company").value.trim();
  const result = await auth(`/signup?redirect_to=${encodeURIComponent(DASHBOARD)}`, {
    email: lastEmail,
    password: input("password").value,
    data: {
      business_type: type,
      business_type_other: type === "other" ? input("other").value.trim() : "",
      company_name: lastCompany,
      owner_name: input("owner").value.trim(),
    },
  });
  button.disabled = false;
  button.textContent = "Create account";
  if (alreadyExists(result)) {
    setError("email", "This email already has an account. Sign in, or use Forgot password on the sign-in page.");
    input("email").focus();
    return;
  }
  if (result.status === 200 && result.data.access_token) {
    // Email confirmation is off: hand the new session to the dashboard, which finishes the set-up.
    const hash = new URLSearchParams({
      access_token: result.data.access_token,
      refresh_token: result.data.refresh_token || "",
      type: "signup",
    });
    location.assign(`${DASHBOARD}#${hash}`);
    return;
  }
  if (result.status === 200) {
    input("password").value = "";
    showStrength();
    showDone();
    return;
  }
  const messages = {
    0: "We couldn't reach the sign-up service. Check your connection and try again.",
    422: "Check your email address, and choose a stronger password.",
    429: "Too many attempts. Please wait a few minutes and try again.",
  };
  formMessage(messages[result.status] || "Your account couldn't be created right now. Please try again.");
}

/** Send the confirmation email again. */
async function resend() {
  const button = input("resend");
  const box = input("resend-msg");
  button.disabled = true;
  const result = await auth(`/resend?redirect_to=${encodeURIComponent(DASHBOARD)}`, {
    type: "signup",
    email: lastEmail,
  });
  button.disabled = false;
  box.hidden = false;
  box.textContent =
    result.status === 200
      ? "Sent again. It can take a minute to arrive."
      : result.status === 429
        ? "Please wait a minute before asking again."
        : "We couldn't send it right now. Please try again.";
}

input("type").addEventListener("change", () => {
  input("other-wrap").hidden = input("type").value !== "other";
});
input("password").addEventListener("input", showStrength);
input("pw-toggle").addEventListener("click", () => {
  const field = input("password");
  const show = field.type === "password";
  field.type = show ? "text" : "password";
  input("pw-toggle").textContent = show ? "Hide" : "Show";
  input("pw-toggle").setAttribute("aria-pressed", String(show));
});
input("resend").addEventListener("click", resend);
input("restart").addEventListener("click", () => {
  input("done-view").hidden = true;
  input("form-view").hidden = false;
  input("email").value = "";
  input("email").focus();
});
form.addEventListener("submit", submit);

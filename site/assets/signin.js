// Owner sign-in, with forgotten-password and new-password steps.
// A successful sign-in keeps the session for this tab and opens the owner's Today screen.
// All text reaches the page through textContent.

import { EMAIL, NOT_CONFIGURED, auth, passwordToggle, say, setError, startOwnerSession } from "./auth.js";

const RESET_RETURN = `${location.origin}/signin/`;
const input = (id) => document.getElementById(id);
const views = ["signin-view", "forgot-view", "reset-view"];
let recoveryToken = "";

/**
 * Show one step and move focus to its heading.
 * @param {string} name The section id.
 */
function show(name) {
  for (const id of views) input(id).hidden = id !== name;
  input(name).querySelector("h1").focus();
}

/**
 * Switch a button between its normal and busy text.
 * @param {string} id The button id.
 * @param {string|null} busyText The busy text, or null to restore the normal text.
 */
function busy(id, busyText) {
  const button = input(id);
  if (busyText) {
    button.dataset.label = button.dataset.label || button.textContent;
    button.textContent = busyText;
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
  button.disabled = Boolean(busyText);
}

/**
 * Sign in with email and password.
 * @param {SubmitEvent} event The submit event.
 */
async function signIn(event) {
  event.preventDefault();
  say("signin-msg", "");
  input("resend-confirm").hidden = true;
  const email = input("email").value.trim();
  setError("email", EMAIL.test(email) ? "" : "Enter an email like name@example.com.");
  setError("password", input("password").value ? "" : "Enter your password.");
  if (!EMAIL.test(email)) return input("email").focus();
  if (!input("password").value) return input("password").focus();
  if (NOT_CONFIGURED) return say("signin-msg", "Sign-in is not configured on this page yet.");
  busy("signin-btn", "Signing in…");
  const result = await auth("/token?grant_type=password", { email, password: input("password").value });
  busy("signin-btn", null);
  if (result.status === 200 && result.data.access_token) {
    input("password").value = "";
    startOwnerSession(result.data, email);
    return;
  }
  const code = result.data.error_code || result.data.code || "";
  if (code === "email_not_confirmed") {
    say("signin-msg", "Please confirm your email first. Check your inbox for the link we sent when you signed up.");
    input("resend-confirm").hidden = false;
    return;
  }
  const messages = {
    0: "We couldn't reach the sign-in service. Check your connection and try again.",
    400: "That email or password isn't right. Try again, or reset your password.",
    429: "Too many attempts. Please wait a few minutes and try again.",
  };
  say("signin-msg", messages[result.status] || "Sign-in isn't available right now. Please try again.");
}

/** Send the sign-up confirmation email again. */
async function resendConfirmation() {
  busy("resend-confirm", "Sending…");
  const result = await auth(`/resend?redirect_to=${encodeURIComponent(`${location.origin}/`)}`, {
    type: "signup",
    email: input("email").value.trim(),
  });
  busy("resend-confirm", null);
  say(
    "signin-msg",
    result.status === 200
      ? "Sent. Open the link in the email, then sign in."
      : "We couldn't send it right now. Please try again.",
  );
  input("resend-confirm").hidden = result.status === 200;
}

/**
 * Send a password reset link.
 * @param {SubmitEvent} event The submit event.
 */
async function sendReset(event) {
  event.preventDefault();
  say("forgot-msg", "");
  const email = input("forgot-email").value.trim();
  setError("forgot-email", EMAIL.test(email) ? "" : "Enter an email like name@example.com.");
  if (!EMAIL.test(email)) return input("forgot-email").focus();
  if (NOT_CONFIGURED) return say("forgot-msg", "Password reset is not configured on this page yet.");
  busy("forgot-send", "Sending…");
  const result = await auth(`/recover?redirect_to=${encodeURIComponent(RESET_RETURN)}`, { email });
  busy("forgot-send", null);
  // The same answer whether or not the email has an account, so the page never reveals who has one.
  say(
    "forgot-msg",
    result.status === 429
      ? "Please wait a minute before asking again."
      : result.status === 0
        ? "We couldn't reach the service. Check your connection and try again."
        : "If that email has an account, a reset link is on its way. It can take a minute to arrive.",
  );
}

/**
 * Save the new password chosen after a reset link.
 * @param {SubmitEvent} event The submit event.
 */
async function saveNewPassword(event) {
  event.preventDefault();
  say("reset-msg", "");
  const password = input("new-password").value;
  setError("new-password", password.length >= 8 ? "" : "Your password needs at least 8 characters.");
  if (password.length < 8) return input("new-password").focus();
  busy("reset-btn", "Saving…");
  const result = await auth("/user", { password }, { method: "PUT", token: recoveryToken });
  busy("reset-btn", null);
  if (result.status === 200) {
    input("new-password").value = "";
    recoveryToken = "";
    show("signin-view");
    say("signin-note", "Password changed. Sign in with your new password.");
    return;
  }
  const code = result.data.error_code || result.data.code || "";
  say(
    "reset-msg",
    code === "same_password"
      ? "Choose a password different from your old one."
      : code === "weak_password"
        ? "Choose a stronger password."
        : "The password couldn't be changed. The link may have expired: ask for a new one.",
  );
}

/** Act on a return from a reset email: #access_token=…&type=recovery, or an expired-link error. */
function readReturn() {
  const hash = new URLSearchParams(location.hash.slice(1));
  if (!hash.size) return;
  history.replaceState(null, "", location.pathname + location.search);
  if (hash.get("type") === "recovery" && hash.get("access_token")) {
    recoveryToken = hash.get("access_token");
    show("reset-view");
    return;
  }
  if (hash.get("error") || hash.get("error_description")) {
    show("forgot-view");
    say("forgot-msg", "That link has expired or was already used. Ask for a new one below.");
  }
}

passwordToggle("pw-toggle", "password");
passwordToggle("new-pw-toggle", "new-password");
input("signin-form").addEventListener("submit", signIn);
input("forgot-form").addEventListener("submit", sendReset);
input("reset-form").addEventListener("submit", saveNewPassword);
input("resend-confirm").addEventListener("click", resendConfirmation);
input("forgot-btn").addEventListener("click", () => {
  input("forgot-email").value = input("email").value.trim();
  say("forgot-msg", "");
  show("forgot-view");
});
input("back-to-signin").addEventListener("click", () => show("signin-view"));
readReturn();

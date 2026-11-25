// Owner "Booking page" screen: choose how the booking page looks (a starting theme, a logo, colours, font and
// corners), see it as customers will, save a draft, publish it or go back to the standard look.
// All text reaches the page through textContent. A look is only ever sent to the API as the fixed fields it accepts.

import { API_URL } from "./config.js";
import { addDays, dateParts, el, fill, today } from "./gn.js";
import { setUpButtonTab } from "./button-code.js";
import { contrast, lookProperties } from "./look.js";
import {
  CORNERS,
  FONTS,
  LOGO_MAX_BYTES,
  STANDARD,
  THEMES,
  draftBody,
  editorState,
  fromServer,
  liveLook,
  logoProblem,
  onAccent,
  readabilityProblems,
} from "./look-model.js";
import { bookingLink, describePlan, notice, owner, startOwnerPage } from "./owner.js";

const input = (id) => document.getElementById(id);

const STEPS = [
  ["pick", "Pick a time"],
  ["details", "Your details"],
  ["done", "Confirmed"],
];

const state = {
  slug: "",
  name: "",
  allowed: true,
  draft: null,
  published: null,
  working: { ...STANDARD },
  tab: "look",
  device: "desktop",
  step: "pick",
  busy: false,
  logoMessage: "",
  logoError: false,
  logoBusy: false,
  resetArmed: false,
};

let buttonTab = null;
let previewHost = null;
let previewContent = null;
let appliedProperties = [];
let resetToken = 0;

/**
 * The address of an uploaded logo.
 * @param {string} id The logo identifier.
 * @returns {string} Its address on the API.
 */
const logoUrl = (id) => `${API_URL}/businesses/${encodeURIComponent(state.slug)}/logos/${id}`;

/**
 * Take in an answer from the API.
 * @param {{allowed: boolean, draft: object|null, published: object|null}} data The editor state.
 */
function take(data) {
  state.allowed = data.allowed === true;
  state.draft = fromServer(data.draft);
  state.published = fromServer(data.published);
}

/**
 * Say what went wrong in plain words.
 * @param {{status: number, data: any}} result The API answer.
 * @param {string} fallback What to say when the API gave no reason.
 * @returns {string} The message.
 */
function reason(result, fallback) {
  if (result.status === 422 && result.data && typeof result.data.detail === "string") return result.data.detail;
  return fallback;
}

/* ---------- the preview ---------- */

/**
 * The header of the previewed page: the logo if there is one, otherwise the business name.
 * @returns {HTMLElement} The header.
 */
function previewHeader() {
  const name = el("h1", {}, state.name);
  const badge = el("div", { class: "badge", "aria-hidden": "true" }, (state.name.trim()[0] || "?").toUpperCase());
  const logo = state.working.logo_id;
  if (logo) {
    const image = el("img", { src: logoUrl(logo), alt: "" });
    image.addEventListener("error", () => {
      badge.classList.remove("has-logo");
      badge.textContent = (state.name.trim()[0] || "?").toUpperCase();
      name.classList.remove("visually-hidden");
    });
    badge.classList.add("has-logo");
    badge.replaceChildren(image);
    name.classList.add("visually-hidden");
  }
  return el("header", { class: "top" }, badge, el("div", {}, name, el("div", { class: "sub" }, "Book online")));
}

/**
 * The step bar, with every step up to the current one lit.
 * @param {number} at The current step, from 0.
 * @returns {HTMLElement} The bar.
 */
function previewSteps(at) {
  return el(
    "ol",
    { class: "steps" },
    ...["Service", "Day and time", "Your details"].map((label, i) => el("li", { class: i <= at ? "lit" : "" }, label)),
  );
}

/**
 * The summary card beside (or under) the steps.
 * @param {string} action What the card offers: a button label, or "change" for the link shown on later steps.
 * @returns {HTMLElement} The card.
 */
function previewSummary(action) {
  return el(
    "div",
    { class: "summary" },
    el(
      "div",
      { class: "line" },
      el(
        "span",
        { class: "grow" },
        el("strong", {}, "Cut and beard trim"),
        el("span", { class: "label" }, `${dateParts(addDays(today("Europe/London"), 2)).long}, 10:00`),
      ),
      el("span", { class: "total" }, "£12"),
    ),
    action === "change"
      ? el("button", { type: "button", class: "link-btn" }, "Change the time")
      : el("button", { type: "button", class: "btn block" }, action),
  );
}

/**
 * The "pick a time" step, with sample services and times.
 * @param {boolean} wide Whether the preview is desktop width.
 * @returns {HTMLElement[]} The steps and choices.
 */
function previewPick(wide) {
  const base = today("Europe/London");
  const days = [1, 2, 3, 4].map((n) => dateParts(addDays(base, n)));
  const services = el(
    "div",
    { class: "choice-list two" },
    ...[
      ["Cut and beard trim", "60 min", "£12", true],
      ["Skin fade", "30 min", "Free", false],
    ].map(([name, length, price, on]) =>
      el(
        "button",
        { type: "button", class: "choice", "aria-pressed": on ? "true" : "false" },
        el("span", { class: "grow" }, el("strong", {}, name), el("span", { class: "meta" }, length)),
        el("span", { class: "price" }, price),
      ),
    ),
  );
  services.style.gridTemplateColumns = wide ? "repeat(2, minmax(0, 1fr))" : "minmax(0, 1fr)";
  const times = el(
    "div",
    { class: "times" },
    ...["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"].map((t) =>
      el(
        "button",
        { type: "button", class: "time", disabled: t === "10:30", "aria-pressed": t === "10:00" ? "true" : "false" },
        t,
      ),
    ),
  );
  times.style.gridTemplateColumns = wide ? "repeat(4, minmax(0, 1fr))" : "repeat(3, minmax(0, 1fr))";
  return [
    previewSteps(1),
    el("section", {}, el("h2", {}, "What would you like to book?"), services),
    el(
      "section",
      {},
      el(
        "div",
        { class: "row-between" },
        el("h2", {}, "When suits you?"),
        el("button", { type: "button", class: "chip-btn" }, "Next available time"),
      ),
      el(
        "div",
        { class: "dates" },
        ...days.map((p, i) =>
          el(
            "button",
            { type: "button", class: "date", "aria-pressed": i === 1 ? "true" : "false" },
            el("span", { class: "dow" }, p.dow),
            el("span", { class: "num" }, p.day),
            el("span", { class: "mon" }, p.month),
          ),
        ),
      ),
      times,
    ),
  ];
}

/**
 * The "your details" step.
 * @returns {HTMLElement[]} The steps and form.
 */
function previewDetails() {
  const field = (label, value) => {
    const box = el("input", { type: "text", tabindex: "-1" });
    box.value = value;
    return el("label", { class: "field" }, label, box);
  };
  return [
    previewSteps(2),
    el(
      "section",
      {},
      el("h2", {}, "Your details"),
      field("Full name", "Sam Taylor"),
      field("Email", "sam@example.com"),
    ),
  ];
}

/**
 * The confirmation after booking.
 * @returns {HTMLElement[]} The confirmation.
 */
function previewDone() {
  return [
    el(
      "section",
      { class: "done" },
      el("div", { class: "tick", "aria-hidden": "true" }, "✓"),
      el("h2", {}, "You're booked"),
      el(
        "div",
        { class: "summary" },
        el("strong", {}, `Cut and beard trim with ${state.name}`),
        el("span", {}, `${dateParts(addDays(today("Europe/London"), 2)).long}, 10:00`),
        el("span", { class: "small" }, "Reference ", el("span", { class: "ref" }, "TMXEXYVPLH")),
      ),
      el("p", { class: "muted" }, "We've emailed the details to sam@example.com."),
      el(
        "div",
        { class: "btn-row split" },
        el("button", { type: "button", class: "btn ghost" }, "Add to calendar"),
        el("button", { type: "button", class: "btn" }, "Move or cancel"),
      ),
    ),
  ];
}

/** Redraw the preview from the working look. */
function drawPreview() {
  const look = state.working;
  const properties = lookProperties({ ...look, on_accent: onAccent(look.accent, contrast) });
  for (const name of appliedProperties) previewHost.style.removeProperty(name);
  appliedProperties = [];
  if (properties) {
    for (const [name, value] of Object.entries(properties)) {
      previewHost.style.setProperty(name, value);
      appliedProperties.push(name);
    }
  }
  const wide = state.device === "desktop";
  previewHost.classList.toggle("phone", !wide);
  const main = el("main", { class: "page" });
  main.style.paddingBottom = "24px";
  if (state.step === "done") {
    fill(main, ...previewDone());
  } else {
    const body = state.step === "pick" ? previewPick(wide) : previewDetails();
    const side = previewSummary(state.step === "pick" ? "Continue to your details" : "Confirm booking");
    main.style.gridTemplateColumns = wide ? "minmax(0, 1fr) 280px" : "minmax(0, 1fr)";
    main.style.alignItems = "start";
    fill(main, el("div", { class: "stack" }, ...body), state.step === "pick" ? side : previewSummary("change"));
    if (state.step === "details")
      main.lastElementChild.append(el("button", { type: "button", class: "btn block" }, "Confirm booking"));
  }
  previewContent.replaceChildren(previewHeader(), main);
}

/* ---------- the controls ---------- */

/**
 * Show a message about the last action.
 * @param {string} message The message, or "".
 * @param {boolean} [good] Whether it is good news.
 */
const say = (message, good = false) => notice("look-msg", message, good);

/** Redraw every control and the preview from the working look. */
function draw() {
  const look = state.working;
  document.querySelectorAll("[data-theme]").forEach((button) => {
    const t = THEMES.find((x) => x.id === button.dataset.theme);
    const on =
      t.accent === look.accent &&
      t.background === look.background &&
      t.text === look.text &&
      t.font === look.font &&
      t.corners === look.corners;
    button.setAttribute("aria-pressed", String(on));
  });
  for (const [id, key, code] of [
    ["c-accent", "accent", "t-accent"],
    ["c-background", "background", "t-background"],
    ["c-text", "text", "t-text"],
  ]) {
    input(id).value = look[key];
    input(code).textContent = look[key];
  }
  input("look-font").value = look.font;
  document
    .querySelectorAll("[data-corners]")
    .forEach((b) => b.setAttribute("aria-selected", String(b.dataset.corners === look.corners)));
  document
    .querySelectorAll("[data-device]")
    .forEach((b) => b.setAttribute("aria-selected", String(b.dataset.device === state.device)));
  document
    .querySelectorAll("[data-step]")
    .forEach((b) => b.setAttribute("aria-selected", String(b.dataset.step === state.step)));

  const thumb = input("logo-thumb");
  if (look.logo_id) {
    const image = el("img", { src: logoUrl(look.logo_id), alt: "Your logo" });
    image.addEventListener("error", () => fill(thumb, el("span", { class: "small muted" }, "Not found")));
    fill(thumb, image);
  } else {
    fill(thumb, el("span", { class: "small muted" }, "None"));
  }
  input("logo-remove").hidden = !look.logo_id;
  input("logo-file").disabled = state.logoBusy;
  const hint = input("logo-hint");
  hint.textContent =
    state.logoMessage ||
    `PNG, JPEG or WebP, up to ${LOGO_MAX_BYTES / 1024} KB. Without a logo your business name is shown.`;
  hint.className = state.logoError ? "small error" : "small muted";

  const problems = readabilityProblems(look, contrast);
  const readable = input("look-readable");
  readable.className = `look-readable ${problems.length ? "bad" : "ok"}`;
  readable.textContent = problems.length
    ? `Can't be published yet: ${problems.join("; ")}.`
    : "Easy to read: text, buttons and highlights all pass.";

  const s = editorState({
    working: look,
    draft: state.draft,
    published: state.published,
    allowed: state.allowed,
    problems,
  });
  const status = input("look-status");
  status.className = s.status === "published" ? "look-status" : "look-status dirty";
  if (s.status === "published") {
    status.textContent = !state.published
      ? "Showing the standard look"
      : state.allowed
        ? "Published"
        : "Paused: your plan doesn't include a branded page, so customers see the standard look";
  } else {
    status.textContent = s.status === "draft-saved" ? "Draft saved · not live yet" : "Unsaved changes";
  }
  input("btn-save").disabled = state.busy || !s.canSave;
  const publish = input("btn-publish");
  publish.textContent = s.needsPlan ? "Choose a plan to publish" : "Publish";
  publish.className = s.needsPlan ? "btn small upgrade" : "btn small";
  publish.disabled = s.needsPlan ? state.busy : state.busy || !s.canPublish;
  input("btn-reset").disabled = state.busy;
  input("btn-reset").textContent = state.resetArmed ? "Really reset?" : "Reset to standard";
  drawPreview();
  if (buttonTab) buttonTab.draw({ look: liveLook(state), allowed: state.allowed });
}

/**
 * Run an action that talks to the API, blocking the buttons meanwhile.
 * @param {() => Promise<void>} action What to do.
 */
async function busy(action) {
  state.busy = true;
  draw();
  try {
    await action();
  } finally {
    state.busy = false;
    draw();
  }
}

/**
 * Save the working look as the draft.
 * @returns {Promise<boolean>} Whether it was saved.
 */
async function saveDraft() {
  const result = await owner(`/businesses/${state.slug}/look/draft`, { method: "PUT", body: draftBody(state.working) });
  if (result.status === 200 && result.data) {
    take(result.data);
    return true;
  }
  say(reason(result, "The draft couldn't be saved. Check your connection and try again."));
  return false;
}

/** Save the draft and say so. */
async function onSave() {
  say("");
  await busy(async () => {
    if (await saveDraft()) {
      say(
        state.allowed
          ? "Draft saved. Customers still see the published look."
          : "Draft saved. Customers see the standard look until you choose a plan and publish.",
        true,
      );
    }
  });
}

/** Publish the working look: save it as the draft first, then make the draft live. */
async function onPublish() {
  say("");
  if (!state.allowed) {
    location.href = "/app/plan/";
    return;
  }
  await busy(async () => {
    if (!(await saveDraft())) return;
    const result = await owner(`/businesses/${state.slug}/look/publish`, { method: "POST" });
    if (result.status === 200 && result.data) {
      take(result.data);
      say("Published. Your booking page now uses this look.", true);
    } else {
      say(reason(result, "It couldn't be published. Check your connection and try again."));
    }
  });
}

/** Go back to the standard look, after a second click to confirm. */
async function onReset() {
  say("");
  if (!state.resetArmed) {
    state.resetArmed = true;
    const token = ++resetToken;
    setTimeout(() => {
      if (token !== resetToken) return;
      state.resetArmed = false;
      draw();
    }, 4000);
    draw();
    return;
  }
  state.resetArmed = false;
  resetToken += 1;
  await busy(async () => {
    const result = await owner(`/businesses/${state.slug}/look/reset`, { method: "POST" });
    if (result.status === 200 && result.data) {
      take(result.data);
      state.working = { ...STANDARD };
      state.logoMessage = "";
      say("Back to the standard look. Your draft, published look and logo are removed.", true);
    } else {
      say(reason(result, "It couldn't be reset. Check your connection and try again."));
    }
  });
}

/**
 * Upload a chosen logo and use it in the working look.
 * @param {File} file The chosen file.
 */
async function onLogo(file) {
  const problem = logoProblem(file);
  state.logoError = Boolean(problem);
  state.logoMessage = problem;
  if (problem) return draw();
  state.logoBusy = true;
  state.logoMessage = "Uploading…";
  draw();
  const result = await owner(`/businesses/${state.slug}/look/logo`, {
    method: "POST",
    raw: { type: file.type, body: await file.arrayBuffer() },
  });
  state.logoBusy = false;
  if (result.status === 201 && result.data && result.data.logo_id) {
    state.working.logo_id = result.data.logo_id;
    state.logoError = false;
    state.logoMessage = `${file.name} · ${file.size < 1024 ? "under 1 KB" : `${Math.round(file.size / 1024)} KB`}. Save a draft to keep it.`;
  } else {
    state.logoError = true;
    state.logoMessage =
      result.status === 413
        ? `That image is too large. The limit is ${LOGO_MAX_BYTES / 1024} KB.`
        : reason(result, "The logo couldn't be uploaded. Check your connection and try again.");
  }
  draw();
}

/**
 * Show the Look tab or the Button tab. The address keeps "#button", so the Today screen can link straight to it.
 * @param {"look"|"button"} name The tab.
 */
function showTab(name) {
  state.tab = name;
  input("look-editor").hidden = name !== "look";
  input("look-button").hidden = name !== "button";
  for (const tab of ["look", "button"]) input(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
  history.replaceState(null, "", `${location.pathname}${location.search}${name === "button" ? "#button" : ""}`);
}

/** Build the controls that do not change: themes, fonts and corners, and wire every control up. */
function build() {
  fill(
    input("themes"),
    ...THEMES.map((t) => {
      const swatch = el("div", { class: "look-swatch" }, el("i"), el("i"));
      swatch.style.background = t.background;
      swatch.children[0].style.background = t.accent;
      swatch.children[1].style.background = t.text;
      const button = el(
        "button",
        { type: "button", class: "look-theme", "data-theme": t.id, "aria-pressed": "false" },
        swatch,
        t.name,
      );
      button.addEventListener("click", () => {
        Object.assign(state.working, {
          accent: t.accent,
          background: t.background,
          text: t.text,
          font: t.font,
          corners: t.corners,
        });
        say("");
        draw();
      });
      return button;
    }),
  );
  fill(input("look-font"), ...FONTS.map(([value, label]) => el("option", { value }, label)));
  input("look-font").addEventListener("change", (e) => {
    state.working.font = e.target.value;
    draw();
  });
  fill(
    input("look-corners"),
    ...CORNERS.map(([value, label]) => {
      const button = el(
        "button",
        { type: "button", role: "tab", "data-corners": value, "aria-selected": "false" },
        label,
      );
      button.addEventListener("click", () => {
        state.working.corners = value;
        draw();
      });
      return button;
    }),
  );
  for (const [id, key] of [
    ["c-accent", "accent"],
    ["c-background", "background"],
    ["c-text", "text"],
  ]) {
    input(id).addEventListener("input", (e) => {
      state.working[key] = e.target.value;
      draw();
    });
  }
  fill(
    input("look-device"),
    ...[
      ["desktop", "Desktop"],
      ["phone", "Phone"],
    ].map(([value, label]) => {
      const button = el(
        "button",
        { type: "button", role: "tab", "data-device": value, "aria-selected": "false" },
        label,
      );
      button.addEventListener("click", () => {
        state.device = value;
        draw();
      });
      return button;
    }),
  );
  fill(
    input("look-step"),
    ...STEPS.map(([value, label]) => {
      const button = el("button", { type: "button", role: "tab", "data-step": value, "aria-selected": "false" }, label);
      button.addEventListener("click", () => {
        state.step = value;
        draw();
      });
      return button;
    }),
  );
  input("logo-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) onLogo(file);
  });
  input("logo-remove").addEventListener("click", () => {
    state.working.logo_id = null;
    state.logoError = false;
    state.logoMessage = "";
    draw();
  });
  input("btn-save").addEventListener("click", onSave);
  input("btn-publish").addEventListener("click", onPublish);
  input("btn-reset").addEventListener("click", onReset);
  input("link-live").href = bookingLink(state.slug);
  buttonTab = setUpButtonTab(state.slug);
  for (const tab of ["look", "button"]) input(`tab-${tab}`).addEventListener("click", () => showTab(tab));

  // The preview lives in a shadow tree so the owner screen's styles cannot reach it, and it uses the customer pages'
  // own stylesheet, so it cannot drift from the real page. It is for looking at, not for using.
  previewHost = input("preview");
  previewHost.inert = true;
  const root = previewHost.attachShadow({ mode: "open" });
  const sheet = document.querySelector('link[rel="stylesheet"][href*="gn.css"]');
  root.append(el("link", { rel: "stylesheet", href: sheet ? sheet.getAttribute("href") : "/assets/gn.css" }));
  previewContent = el("div");
  root.append(previewContent);
}

/** Start the page. */
async function start() {
  const page = await startOwnerPage("look");
  if (!page) return;
  state.slug = page.slug;
  state.name = page.business.name;
  const plan = describePlan(page.business);
  const result = await owner(`/businesses/${state.slug}/look`);
  if (result.status !== 200 || !result.data) {
    notice("app-msg", "The booking page look couldn't load. Check your connection and refresh the page.");
    return;
  }
  take(result.data);
  state.working = { ...(state.draft || state.published || STANDARD) };
  build();
  input("look-tabs").hidden = false;
  showTab(location.hash === "#button" ? "button" : "look");
  if (!state.allowed) {
    fill(
      input("look-plan"),
      "Your plan doesn't include a branded booking page. You can try looks and save a draft. ",
      el("a", { href: "/app/plan/" }, "Choose a plan to publish →"),
    );
    input("look-plan").className = "notice warn";
    input("look-plan").hidden = false;
  } else if (plan.trialLeft > 0 && page.business.plan === "free") {
    input("look-plan").textContent =
      `A branded booking page is included in your free trial (${plan.trialLeft} ${plan.trialLeft === 1 ? "day" : "days"} left) and in every paid plan.`;
    input("look-plan").className = "notice";
    input("look-plan").hidden = false;
  }
  draw();
}

start();

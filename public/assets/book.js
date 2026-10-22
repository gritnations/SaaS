// The customer booking page: pick a service, a day and a time, give your details, book.

import { addDays, api, contactBlock, dateParts, downloadCalendar, el, fill, money, showBusiness, today } from "./gn.js";

const CHUNK_DAYS = 14;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE = /^[0-9+()\- ]{5,32}$/;
const PRIVACY_URL = "https://gritnations.co.uk/privacy.html";

const flow = document.getElementById("flow");
const aside = document.getElementById("aside");

const state = {
  slug: "",
  business: null,
  services: [],
  service: null,
  days: [],
  nextStart: "",
  lastDay: "",
  loading: false,
  day: 0,
  time: null,
  step: "pick",
  tried: false,
  pay: "online",
  form: { name: "", email: "", phone: "", note: "" },
  busy: false,
  problem: "",
  booking: null,
  returned: null,
};

/**
 * Read the business slug from /book/{slug} or ?b={slug}.
 * @returns {string} The slug, or "" when there is none.
 */
function readSlug() {
  const fromPath = location.pathname.match(/^\/book\/([a-z0-9][a-z0-9-]{0,38}[a-z0-9])\/?$/);
  if (fromPath) return fromPath[1];
  const fromQuery = new URLSearchParams(location.search).get("b") || "";
  return /^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$/.test(fromQuery) ? fromQuery : "";
}

/**
 * Read a return from the payment page: #paid=REF or #unpaid=REF.
 * @returns {{paid: boolean, reference: string}|null} The return, or null.
 */
function readReturn() {
  const match = location.hash.match(/^#(paid|unpaid)=([A-Za-z0-9]{6,64})$/);
  if (!match) return null;
  history.replaceState(null, "", location.pathname + location.search);
  return { paid: match[1] === "paid", reference: match[2] };
}

/** @returns {object|null} The selected day, or null. */
function selectedDay() {
  return state.days[state.day] || null;
}

/** @returns {string} The chosen session as people read it, or a prompt. */
function whenText() {
  const day = selectedDay();
  if (!day || !state.time) return "Pick a day and time";
  const slot = day.slots.find((s) => s.time === state.time);
  return `${dateParts(day.date).long}, ${slot ? slot.label : state.time}`;
}

/** @returns {string[]} How a paid service can be paid at this business. */
function payOptions() {
  return state.business ? state.business.payment_options : [];
}

/** @returns {boolean} Whether the chosen service costs money. */
function isPaid() {
  return Boolean(state.service && !state.service.free);
}

/** @returns {string} The price of the chosen service. */
function priceText() {
  return state.service ? money(state.service.price_minor, state.business.currency) : "";
}

/**
 * Load the next run of days for the chosen service, up to the booking window.
 * Keeps loading while a run comes back empty, so a closed fortnight does not look like "no times".
 */
async function loadMore() {
  if (state.loading || !state.service || state.nextStart > state.lastDay) return;
  state.loading = true;
  render();
  const before = state.days.length;
  while (state.nextStart <= state.lastDay) {
    const start = state.nextStart;
    const path =
      `/businesses/${state.slug}/availability?service=${encodeURIComponent(state.service.slug)}` +
      `&start=${start}&days=${CHUNK_DAYS}`;
    const answer = await api(path);
    if (!answer.ok) {
      state.problem = "We couldn't load the times. Check your connection and try again.";
      break;
    }
    state.nextStart = addDays(start, CHUNK_DAYS);
    state.days.push(...answer.data.days);
    if (state.days.length > before) break;
  }
  if (before === 0) {
    const first = state.days.findIndex((d) => d.slots.some((s) => s.available));
    state.day = first < 0 ? 0 : first;
  }
  state.loading = false;
  render();
}

/**
 * Choose a service and load its times.
 * @param {object} service The service.
 */
function chooseService(service) {
  if (state.service && state.service.slug === service.slug) return;
  state.service = service;
  state.days = [];
  state.day = 0;
  state.time = null;
  state.tried = false;
  state.problem = "";
  const tz = state.business.time_zone;
  state.nextStart = today(tz);
  state.lastDay = addDays(today(tz), Math.max(state.business.window_days, 1) - 1);
  loadMore();
}

/** Jump to the first available time. */
function pickNext() {
  for (let i = 0; i < state.days.length; i += 1) {
    const slot = state.days[i].slots.find((s) => s.available);
    if (slot) {
      state.day = i;
      state.time = slot.time;
      state.tried = false;
      render();
      return;
    }
  }
}

/** @returns {HTMLElement} The step bar. */
function stepBar() {
  const at = state.step === "pick" ? (state.time ? 1 : 0) : 2;
  const names = ["Service", "Day and time", "Your details"];
  return el(
    "ol",
    { class: "steps", "aria-label": "Booking progress" },
    ...names.map((name, i) =>
      el("li", { class: i <= at ? "lit" : "", "aria-current": i === at ? "step" : null }, name),
    ),
  );
}

/** @returns {HTMLElement} The service choices. */
function serviceSection() {
  const currency = state.business.currency;
  return el(
    "section",
    { "aria-labelledby": "svc-h" },
    el("h2", { id: "svc-h" }, "What would you like to book?"),
    el(
      "div",
      { class: "choice-list two" },
      ...state.services.map((s) =>
        el(
          "button",
          {
            type: "button",
            class: "choice",
            "aria-pressed": state.service && state.service.slug === s.slug ? "true" : "false",
            onclick: () => chooseService(s),
          },
          el(
            "span",
            { class: "grow" },
            el("strong", {}, s.name),
            el("span", { class: "meta" }, `${s.duration_minutes} min`),
          ),
          el("span", { class: "price" }, money(s.price_minor, currency)),
        ),
      ),
    ),
  );
}

/** @returns {HTMLElement|null} The day strip and time grid, or null before a service is chosen. */
function timeSection() {
  if (!state.service) return null;
  const head = el(
    "div",
    { class: "row-between" },
    el("h2", { id: "time-h" }, "When suits you?"),
    state.days.some((d) => d.slots.some((s) => s.available))
      ? el("button", { type: "button", class: "chip-btn", onclick: pickNext }, "Next available time")
      : null,
  );
  const parts = [head];
  if (state.problem) parts.push(el("p", { class: "notice warn", role: "alert" }, state.problem));
  if (!state.days.length) {
    parts.push(
      el(
        "p",
        { class: state.loading ? "loading" : "notice", role: "status" },
        state.loading ? "Finding times…" : "There are no times to book right now. Please check back soon.",
      ),
    );
    return el("section", { "aria-labelledby": "time-h" }, ...parts);
  }
  const strip = el(
    "div",
    { class: "dates", role: "group", "aria-label": "Days" },
    ...state.days.map((d, i) => {
      const p = dateParts(d.date);
      return el(
        "button",
        {
          type: "button",
          class: "date",
          "aria-pressed": i === state.day ? "true" : "false",
          "aria-label": p.long,
          onclick: () => {
            state.day = i;
            state.time = null;
            render();
          },
        },
        el("span", { class: "dow" }, p.dow),
        el("span", { class: "num" }, p.day),
        el("span", { class: "mon" }, p.month),
      );
    }),
    state.nextStart <= state.lastDay
      ? el(
          "button",
          { type: "button", class: "date more", onclick: loadMore, disabled: state.loading },
          state.loading ? "Loading…" : "More dates",
        )
      : null,
  );
  const day = selectedDay();
  const grid = el(
    "div",
    { class: "times", role: "group", "aria-label": "Times" },
    ...day.slots.map((s) =>
      el(
        "button",
        {
          type: "button",
          class: "time",
          disabled: !s.available,
          "aria-pressed": state.time === s.time ? "true" : "false",
          "aria-label": s.available ? s.label : `${s.label}, taken`,
          onclick: () => {
            state.time = s.time;
            state.tried = false;
            render();
          },
        },
        s.time,
      ),
    ),
  );
  return el(
    "section",
    { "aria-labelledby": "time-h" },
    ...parts,
    strip,
    el(
      "p",
      { class: "small muted" },
      `Times are ${state.business.time_zone.replace("_", " ")} time. Crossed-out times are taken.`,
    ),
    grid,
  );
}

/**
 * A labelled text field bound to the form state.
 * @param {string} key The form key.
 * @param {string} label The label.
 * @param {object} options Input type, autocomplete, error text and whether it is a textarea.
 * @returns {HTMLElement} The field.
 */
function field(key, label, options) {
  const error = options.error;
  const id = `f-${key}`;
  const control = el(options.textarea ? "textarea" : "input", {
    id,
    name: key,
    type: options.textarea ? null : options.type,
    autocomplete: options.autocomplete,
    maxlength: options.max,
    "aria-invalid": error ? "true" : "false",
    "aria-describedby": error ? `${id}-err` : null,
    oninput: (event) => {
      state.form[key] = event.target.value;
    },
  });
  control.value = state.form[key];
  return el(
    "label",
    { class: "field", for: id },
    label,
    control,
    error ? el("span", { class: "error", id: `${id}-err`, role: "alert" }, error) : null,
  );
}

/** @returns {object} Field errors, keyed by field, shown after a booking attempt. */
function formErrors() {
  if (!state.tried) return {};
  const f = state.form;
  const errors = {};
  if (!f.name.trim()) errors.name = "Enter your name so the business knows who is coming.";
  if (!EMAIL.test(f.email.trim())) errors.email = "Enter an email like name@example.com. Your confirmation goes there.";
  if (f.phone.trim() && !PHONE.test(f.phone.trim())) errors.phone = "Use digits, spaces and + only.";
  return errors;
}

/** @returns {HTMLElement|null} How the customer pays, for a paid service. */
function paySection() {
  if (!isPaid()) return null;
  const options = payOptions();
  const price = priceText();
  if (!options.length) {
    return el(
      "p",
      { class: "notice warn" },
      `${state.service.name} can't be booked online right now. Please contact ${state.business.name}.`,
    );
  }
  if (options.length === 1) {
    return el(
      "p",
      { class: "notice" },
      options[0] === "online"
        ? `You'll pay ${price} by card on Stripe's secure page. Your time is held for 30 minutes while you pay.`
        : `You'll pay ${price} at the venue.`,
    );
  }
  const choices = [
    { id: "online", title: "Pay online now", note: "Card or wallet on Stripe's secure page" },
    { id: "venue", title: "Pay at the venue", note: "When you arrive" },
  ];
  return el(
    "fieldset",
    {},
    el("legend", {}, `How would you like to pay ${price}?`),
    el(
      "div",
      { class: "choice-list two" },
      ...choices.map((c) =>
        el(
          "button",
          {
            type: "button",
            class: "choice",
            "aria-pressed": state.pay === c.id ? "true" : "false",
            onclick: () => {
              state.pay = c.id;
              render();
            },
          },
          el("span", { class: "grow" }, el("strong", {}, c.title), el("span", { class: "meta" }, c.note)),
        ),
      ),
    ),
  );
}

/** @returns {string} The label of the book button. */
function bookLabel() {
  if (!isPaid()) return "Confirm booking";
  const options = payOptions();
  const online = options.length > 1 ? state.pay === "online" : options[0] === "online";
  return online ? `Book and pay ${priceText()}` : `Book, pay ${priceText()} at the venue`;
}

/** @returns {HTMLElement} The details step. */
function detailsSection() {
  const errors = formErrors();
  const blocked = isPaid() && !payOptions().length;
  return el(
    "section",
    { "aria-labelledby": "details-h" },
    el("h2", { id: "details-h" }, "Your details"),
    field("name", "Your name", { type: "text", autocomplete: "name", max: 100, error: errors.name }),
    field("email", "Email", { type: "email", autocomplete: "email", max: 254, error: errors.email }),
    field("phone", "Phone (optional)", { type: "tel", autocomplete: "tel", max: 32, error: errors.phone }),
    field("note", "Anything we should know? (optional)", { textarea: true, max: 1000 }),
    paySection(),
    state.problem ? el("p", { class: "notice warn", role: "alert" }, state.problem) : null,
    el(
      "p",
      { class: "small muted" },
      `We share your details only with ${state.business.name} for this booking. You'll get an email with a link to change or cancel. `,
      el("a", { href: PRIVACY_URL, target: "_blank", rel: "noopener" }, "Privacy notice"),
    ),
    el(
      "div",
      { class: "btn-row split" },
      el("button", { type: "button", class: "btn ghost", onclick: back }, "← Back"),
      el(
        "button",
        { type: "button", class: "btn", onclick: book, disabled: state.busy || blocked },
        state.busy ? "Booking…" : bookLabel(),
      ),
    ),
  );
}

/** Go back to choosing a time. */
function back() {
  state.step = "pick";
  state.problem = "";
  render();
}

/** Continue from the time to the details, or say what is missing. */
function next() {
  if (!state.time) {
    state.tried = true;
    render();
    return;
  }
  state.step = "details";
  state.tried = false;
  state.problem = "";
  render();
  document.getElementById("details-h")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** Send the booking. */
async function book() {
  state.tried = true;
  if (Object.keys(formErrors()).length) {
    render();
    document.querySelector('[aria-invalid="true"]')?.focus();
    return;
  }
  state.busy = true;
  state.problem = "";
  render();
  const f = state.form;
  const body = {
    service: state.service.slug,
    date: selectedDay().date,
    time: state.time,
    name: f.name.trim(),
    email: f.email.trim(),
  };
  if (f.phone.trim()) body.phone = f.phone.trim();
  if (f.note.trim()) body.note = f.note.trim();
  if (isPaid() && payOptions().length > 1) body.pay = state.pay;
  const answer = await api(`/businesses/${state.slug}/bookings`, { method: "POST", body });
  state.busy = false;
  if (answer.ok && answer.data.checkout_url) {
    location.assign(answer.data.checkout_url);
    return;
  }
  if (answer.ok) {
    state.booking = answer.data;
    state.step = "done";
    render();
    window.scrollTo({ top: 0 });
    return;
  }
  if (answer.status === 409) {
    state.problem = "Sorry, that time was just taken. Pick another time.";
    state.step = "pick";
    state.time = null;
    const keep = state.service;
    state.service = null;
    chooseService(keep);
    return;
  }
  if (answer.status === 402) state.problem = "Online payment isn't available right now. Please try again later.";
  else if (answer.status === 422) state.problem = "Please check your details and try again.";
  else state.problem = "We couldn't complete the booking. Check your connection and try again.";
  render();
}

/** @returns {HTMLElement} The summary card, with the continue button while picking. */
function summaryCard() {
  const picking = state.step === "pick";
  return el(
    "div",
    { class: "summary sticky" },
    el(
      "div",
      { class: "line" },
      el(
        "span",
        { class: "grow" },
        el("strong", {}, state.service ? state.service.name : "Choose a service"),
        el("span", { class: "label" }, whenText()),
      ),
      el("span", { class: "total" }, priceText()),
    ),
    picking
      ? el(
          "button",
          { type: "button", class: "btn block", onclick: next, disabled: !state.service },
          "Continue to your details",
        )
      : el("button", { type: "button", class: "link-btn", onclick: back }, "Change the time"),
    picking && state.tried && !state.time
      ? el("span", { class: "hint", role: "status" }, "Pick a time to continue.")
      : null,
  );
}

/** @returns {HTMLElement} The confirmation after a free or pay-at-venue booking. */
function doneSection() {
  const b = state.booking;
  const manage = b.manage_token ? `/manage/#${state.slug}.${b.reference}.${b.manage_token}` : null;
  const paidNote =
    b.payment_status === "at_venue"
      ? `Please pay ${money(b.service.price_minor, state.business.currency)} when you arrive.`
      : b.service.free
        ? "This booking is free."
        : "";
  return el(
    "section",
    { class: "done", "aria-labelledby": "done-h" },
    el("div", { class: "tick", "aria-hidden": "true" }, "✓"),
    el("h2", { id: "done-h", tabindex: "-1" }, "You're booked"),
    el(
      "div",
      { class: "summary" },
      el("strong", {}, `${b.service.name} with ${state.business.name}`),
      el("span", {}, `${dateParts(b.date).long}, ${b.label}`),
      paidNote ? el("span", {}, paidNote) : null,
      el("span", { class: "small" }, "Reference ", el("span", { class: "ref" }, b.reference)),
    ),
    el("p", { class: "muted" }, `We've emailed the details to ${state.form.email.trim()}.`),
    el(
      "div",
      { class: "btn-row split" },
      el(
        "button",
        {
          type: "button",
          class: "btn ghost",
          onclick: () =>
            downloadCalendar({
              reference: b.reference,
              title: `${b.service.name}, ${state.business.name}`,
              startsAt: b.starts_at,
              minutes: b.service.duration_minutes,
              location: state.business.address || "",
            }),
        },
        "Add to calendar",
      ),
      manage ? el("a", { class: "btn ghost", href: manage }, "Change or cancel") : null,
    ),
    el("button", { type: "button", class: "chip-btn", onclick: restart }, "Book something else"),
  );
}

/** @returns {HTMLElement} What a customer sees after coming back from the payment page. */
function returnSection() {
  const r = state.returned;
  if (r.paid) {
    return el(
      "section",
      { class: "done", "aria-labelledby": "done-h" },
      el("div", { class: "tick", "aria-hidden": "true" }, "✓"),
      el("h2", { id: "done-h", tabindex: "-1" }, "Payment received"),
      el(
        "p",
        {},
        `We're confirming your booking with ${state.business.name} now. You'll get an email with the details and a link to change or cancel.`,
      ),
      el("p", { class: "small" }, "Reference ", el("span", { class: "ref" }, r.reference)),
      el("button", { type: "button", class: "btn ghost", onclick: restart }, "Book something else"),
    );
  }
  return el(
    "section",
    { class: "done", "aria-labelledby": "done-h" },
    el("h2", { id: "done-h", tabindex: "-1" }, "Payment not finished"),
    el(
      "p",
      {},
      "You left the payment page, so this booking isn't confirmed and nothing was charged. The time is released within 30 minutes.",
    ),
    el("button", { type: "button", class: "btn", onclick: restart }, "Choose a time"),
  );
}

/** Start a new booking. */
function restart() {
  state.step = "pick";
  state.booking = null;
  state.returned = null;
  state.time = null;
  state.problem = "";
  const keep = state.service;
  state.service = null;
  if (keep) chooseService(keep);
  else render();
}

/** Draw the page for the current state. */
function render() {
  const contact = state.business ? contactBlock(state.business) : null;
  if (state.returned) {
    fill(flow, returnSection());
    fill(aside, contact);
    return;
  }
  if (state.step === "done") {
    fill(flow, doneSection());
    fill(aside, contact);
    return;
  }
  const main = state.step === "pick" ? [serviceSection(), timeSection()] : [detailsSection()];
  fill(flow, el("div", { class: "stack" }, stepBar(), ...main));
  fill(aside, el("div", { class: "stack tight" }, summaryCard(), contact));
}

/** Load the business and start. */
async function start() {
  state.slug = readSlug();
  state.returned = readReturn();
  if (!state.slug) {
    showBusiness("Booking", "Booking link not found");
    fill(
      flow,
      el(
        "p",
        { class: "notice warn", role: "alert" },
        "This booking link is incomplete. Please check it with the business.",
      ),
    );
    return;
  }
  const answer = await api(`/businesses/${state.slug}/services`);
  if (answer.status === 404) {
    showBusiness("Booking", "Booking page not found");
    fill(
      flow,
      el(
        "p",
        { class: "notice warn", role: "alert" },
        "We couldn't find this booking page. Please check the link with the business.",
      ),
    );
    return;
  }
  if (!answer.ok) {
    showBusiness("Booking", "Booking unavailable");
    fill(
      flow,
      el(
        "p",
        { class: "notice warn", role: "alert" },
        "Booking is unavailable right now. Please try again in a few minutes.",
      ),
    );
    return;
  }
  state.business = answer.data.business;
  state.services = answer.data.services;
  state.pay = payOptions().includes("online") ? "online" : "venue";
  showBusiness(state.business.name, `Book with ${state.business.name}`);
  document.getElementById("business-sub").textContent = "Book online";
  render();
  if (state.services.length === 1 && !state.returned) chooseService(state.services[0]);
}

start();

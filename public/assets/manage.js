// The customer manage page: view, move or cancel one booking with the private code from the email link.
// The code stays in the address after "#", so it is never sent to a server in a URL. It goes in request bodies.

import { addDays, api, contactBlock, dateParts, downloadCalendar, el, fill, money, showBusiness, today } from "./gn.js";

const CHUNK_DAYS = 14;
const flow = document.getElementById("flow");
const aside = document.getElementById("aside");

const state = {
  slug: "",
  reference: "",
  token: "",
  booking: null,
  business: null,
  mode: "view",
  days: [],
  nextStart: "",
  lastDay: "",
  loading: false,
  day: 0,
  time: null,
  busy: false,
  problem: "",
  message: "",
};

/**
 * Read the link: #{slug}.{reference}.{token}, or the older #manage={slug}.{reference}.{token}.
 * @returns {boolean} Whether the link is complete.
 */
function readLink() {
  const raw = location.hash.replace(/^#(manage=)?/, "");
  const match = raw.match(/^([a-z0-9][a-z0-9-]{0,38}[a-z0-9])\.([A-Za-z0-9]{10})\.([0-9a-f]{48})$/);
  if (!match) return false;
  [, state.slug, state.reference, state.token] = match;
  return true;
}

/**
 * Format an instant in the business's time zone.
 * @param {string} iso An ISO 8601 instant.
 * @returns {string} For example "Thursday 22 October at 10:30".
 */
function when(iso) {
  const zone = state.booking.time_zone;
  const date = new Intl.DateTimeFormat("en-GB", { timeZone: zone, weekday: "long", day: "numeric", month: "long" });
  const time = new Intl.DateTimeFormat("en-GB", { timeZone: zone, hour: "2-digit", minute: "2-digit" });
  const at = new Date(iso);
  return `${date.format(at)} at ${time.format(at)}`;
}

/**
 * Turn a failed answer into words.
 * @param {{status: number, data: any}} answer The answer.
 * @param {string} conflict What a 409 means for this action.
 * @returns {string} The message.
 */
function failure(answer, conflict) {
  if (answer.status === 404) return "This link doesn't match a booking. It may have been mistyped.";
  if (answer.status === 409) return (answer.data && answer.data.detail) || conflict;
  return "We couldn't reach the booking service. Check your connection and try again.";
}

/** Load more days for moving the booking. */
async function loadMore() {
  if (state.loading || state.nextStart > state.lastDay) return;
  state.loading = true;
  render();
  const before = state.days.length;
  while (state.nextStart <= state.lastDay) {
    const start = state.nextStart;
    const answer = await api(
      `/businesses/${state.slug}/availability?service=${encodeURIComponent(state.booking.service.slug)}` +
        `&start=${start}&days=${CHUNK_DAYS}`,
    );
    if (!answer.ok) {
      state.problem = "We couldn't load the times. Try again.";
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

/** Open the move picker. */
function startMove() {
  state.mode = "move";
  state.problem = "";
  state.message = "";
  state.days = [];
  state.time = null;
  const zone = state.booking.time_zone;
  const windowDays = state.business ? state.business.window_days : 14;
  state.nextStart = today(zone);
  state.lastDay = addDays(today(zone), Math.max(windowDays, 1) - 1);
  loadMore();
}

/** Send the move. */
async function confirmMove() {
  const day = state.days[state.day];
  if (!day || !state.time) return;
  state.busy = true;
  render();
  const answer = await api(`/businesses/${state.slug}/bookings/${state.reference}/move`, {
    method: "POST",
    body: { token: state.token, date: day.date, time: state.time },
  });
  state.busy = false;
  if (answer.ok) {
    state.booking = answer.data;
    state.mode = "view";
    state.message = "Your booking has moved. We've emailed the new details.";
  } else {
    state.problem = failure(answer, "That time was just taken. Pick another.");
    if (answer.status === 409) {
      state.days = [];
      state.time = null;
      state.nextStart = today(state.booking.time_zone);
      loadMore();
      return;
    }
  }
  render();
}

/** Send the cancellation. */
async function confirmCancel() {
  state.busy = true;
  render();
  const answer = await api(`/businesses/${state.slug}/bookings/${state.reference}/cancel`, {
    method: "POST",
    body: { token: state.token },
  });
  state.busy = false;
  if (answer.ok) {
    state.booking = answer.data;
    state.mode = "view";
    state.message = "Your booking is cancelled. We've emailed a confirmation.";
  } else {
    state.problem = failure(answer, "This booking can no longer be cancelled online.");
  }
  render();
}

/** @returns {HTMLElement} The booking card. */
function bookingCard() {
  const b = state.booking;
  const cancelled = b.status === "cancelled";
  const currency = state.business ? state.business.currency : "GBP";
  return el(
    "div",
    { class: "summary" },
    el(
      "div",
      { class: "line" },
      el(
        "span",
        { class: "grow" },
        el("strong", {}, b.service.name),
        el("span", { class: "label" }, `with ${b.business_name}`),
      ),
      el("span", { class: cancelled ? "status-chip off" : "status-chip" }, cancelled ? "Cancelled" : "Confirmed"),
    ),
    el("span", {}, `${dateParts(b.date).long}, ${b.label}`),
    b.service.free ? null : el("span", { class: "small" }, money(b.service.price_minor, currency)),
    el("span", { class: "small" }, "Reference ", el("span", { class: "ref" }, b.reference)),
  );
}

/** @returns {HTMLElement[]} What can be done with a confirmed booking. */
function actions() {
  const b = state.booking;
  const parts = [];
  if (b.can_move && b.can_cancel && b.move_deadline === b.cancel_deadline) {
    parts.push(el("p", { class: "small muted" }, `You can change or cancel online until ${when(b.move_deadline)}.`));
  } else {
    if (b.can_move) {
      parts.push(el("p", { class: "small muted" }, `You can change the time online until ${when(b.move_deadline)}.`));
    }
    if (b.can_cancel) {
      parts.push(el("p", { class: "small muted" }, `You can cancel online until ${when(b.cancel_deadline)}.`));
    }
  }
  if (!b.can_move && !b.can_cancel) {
    parts.push(
      el(
        "p",
        { class: "notice" },
        `Online changes have closed for this booking. To change it, please contact ${b.business_name}${state.business && (state.business.phone || state.business.email) ? " using the details below" : ""}.`,
      ),
    );
  }
  parts.push(
    el(
      "div",
      { class: "btn-row split" },
      b.can_move ? el("button", { type: "button", class: "btn", onclick: startMove }, "Change the time") : null,
      el(
        "button",
        {
          type: "button",
          class: "btn ghost",
          onclick: () =>
            downloadCalendar({
              reference: b.reference,
              title: `${b.service.name}, ${b.business_name}`,
              startsAt: b.starts_at,
              minutes: b.service.duration_minutes,
              location: (state.business && state.business.address) || "",
            }),
        },
        "Add to calendar",
      ),
    ),
  );
  if (b.can_cancel) {
    parts.push(
      el(
        "button",
        {
          type: "button",
          class: "chip-btn",
          onclick: () => {
            state.mode = "cancel";
            state.problem = "";
            state.message = "";
            render();
          },
        },
        "Cancel this booking",
      ),
    );
  }
  return parts;
}

/** @returns {HTMLElement} The cancel confirmation. */
function cancelPanel() {
  const b = state.booking;
  return el(
    "section",
    { class: "panel stack tight", "aria-labelledby": "cancel-h" },
    el("h2", { id: "cancel-h" }, "Cancel this booking?"),
    el("p", { class: "muted" }, `${b.service.name} on ${dateParts(b.date).long}, ${b.label}. This can't be undone.`),
    el(
      "div",
      { class: "btn-row split" },
      el(
        "button",
        { type: "button", class: "btn", onclick: confirmCancel, disabled: state.busy },
        state.busy ? "Cancelling…" : "Yes, cancel it",
      ),
      el(
        "button",
        {
          type: "button",
          class: "btn ghost",
          onclick: () => {
            state.mode = "view";
            render();
          },
        },
        "Keep my booking",
      ),
    ),
  );
}

/** @returns {HTMLElement} The move picker. */
function movePanel() {
  const parts = [el("h2", { id: "move-h" }, "Pick a new time")];
  if (!state.days.length) {
    parts.push(
      el(
        "p",
        { class: state.loading ? "loading" : "notice", role: "status" },
        state.loading ? "Finding times…" : "There are no other times to choose right now.",
      ),
    );
  } else {
    parts.push(
      el(
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
      ),
      el(
        "div",
        { class: "times", role: "group", "aria-label": "Times" },
        ...state.days[state.day].slots.map((s) =>
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
                render();
              },
            },
            s.time,
          ),
        ),
      ),
    );
  }
  const day = state.days[state.day];
  parts.push(
    el(
      "div",
      { class: "btn-row split" },
      el(
        "button",
        { type: "button", class: "btn", onclick: confirmMove, disabled: !state.time || state.busy },
        state.busy
          ? "Moving…"
          : state.time && day
            ? `Move to ${dateParts(day.date).dow} ${dateParts(day.date).day}, ${state.time}`
            : "Pick a time",
      ),
      el(
        "button",
        {
          type: "button",
          class: "btn ghost",
          onclick: () => {
            state.mode = "view";
            state.problem = "";
            render();
          },
        },
        "Keep the current time",
      ),
    ),
  );
  return el("section", { class: "panel stack tight", "aria-labelledby": "move-h" }, ...parts);
}

/** Draw the page for the current state. */
function render() {
  const b = state.booking;
  const parts = [
    el("h2", { id: "booking-h" }, b.status === "cancelled" ? "Your booking is cancelled" : "Your booking"),
    bookingCard(),
  ];
  if (state.message) parts.push(el("p", { class: "notice", role: "status" }, state.message));
  if (state.problem) parts.push(el("p", { class: "notice warn", role: "alert" }, state.problem));
  if (b.status === "cancelled") {
    parts.push(el("a", { class: "btn", href: `/book/${state.slug}` }, `Book again with ${b.business_name}`));
  } else if (state.mode === "move") {
    parts.push(movePanel());
  } else if (state.mode === "cancel") {
    parts.push(cancelPanel());
  } else {
    parts.push(...actions());
  }
  fill(flow, el("div", { class: "stack tight" }, ...parts));
  fill(aside, state.business ? contactBlock(state.business) : null);
}

/** Load the booking and start. */
async function start() {
  if (!readLink()) {
    showBusiness("Booking", "Booking link incomplete");
    fill(
      flow,
      el(
        "p",
        { class: "notice warn", role: "alert" },
        "This link is incomplete or damaged. Open it again from your booking email, or copy the whole link into your browser.",
      ),
    );
    return;
  }
  const [view, services] = await Promise.all([
    api(`/businesses/${state.slug}/bookings/${state.reference}/manage`, {
      method: "POST",
      body: { token: state.token },
    }),
    api(`/businesses/${state.slug}/services`),
  ]);
  if (services.ok) state.business = services.data.business;
  if (!view.ok) {
    showBusiness(state.business ? state.business.name : "Booking", "Booking not found");
    fill(flow, el("p", { class: "notice warn", role: "alert" }, failure(view, "")));
    fill(aside, state.business ? contactBlock(state.business) : null);
    return;
  }
  state.booking = view.data;
  showBusiness(state.booking.business_name, `Your booking with ${state.booking.business_name}`);
  render();
}

// Opening another manage link in the same tab only changes the part after "#", so start again.
window.addEventListener("hashchange", () => location.reload());
start();

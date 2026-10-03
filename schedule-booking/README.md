# Booking backend

Firebase Cloud Functions (2nd gen, Node 22, `us-central1`) + Firestore backend for
the booking flow on `Website-online-booking/pages/book-a-session.html`. Supports
**multiple services** (each with its own duration and price) and **free or paid
booking per service** — a service with `amount: 0` confirms instantly with no
payment step, so the same codebase serves clients who only want scheduling.

Firebase project: `your-project-id`.

## Status

| Piece | State |
|---|---|
| Multi-service + free/paid modes, Firestore-driven schedule, overlap-safe reservation | **Built and tested locally — not yet deployed** (see "Deploying this version") |
| Admin panel (login, bookings, services, availability) | **Built and tested locally — not yet deployed** (see "Admin panel") |
| Cancel / reschedule (customer page + admin button), refunds, calendar + emails | **Built and tested locally — not yet deployed** (see "Cancel, reschedule and refunds") |
| Real Paymob / Resend credentials | Not set up yet — all 5 secrets are placeholder text |

What's *deployed* right now is the earlier single-service version. It still works,
but the updated `book-a-session.html` calls `listServices`, which only exists in
this newer version — so deploy + seed together (below) before pointing real
traffic at the new page.

## Functions

| Function | Trigger | Purpose |
|---|---|---|
| `listServices` | HTTP GET | Bookable services + prices (public) |
| `availableSlots?serviceId=` | HTTP GET | Slot grid for one service (public) |
| `bookingRequest` | HTTP POST | Reserve a slot. Free service → confirmed + emailed immediately. Paid → returns a checkout URL |
| `paymobWebhookIngest` | HTTP POST | Verifies Paymob's HMAC + shared-secret header, relays to Pub/Sub. Holds only the HMAC secrets (least privilege) |
| `processPaymobEvent` | Pub/Sub | Confirms the paid booking, sends emails; if the customer paid for a time that was taken meanwhile, flags a refund and emails the owner instead of double-booking |
| `bookingStatus?bookingId=` | HTTP GET | Id-only status for the page a customer returns to after paying (no personal data) |
| `manageBooking`, `manageSlots` | HTTP GET | The manage page: the booking, and free times for moving it. Secured by the private link token |
| `manageCancel`, `manageReschedule` | HTTP POST | Cancel (refunds if paid) / move to another free time, same length. Token-secured |
| `adminCancelBooking` | HTTP POST | The admin panel's Cancel button. Needs a Firebase sign-in token from an admin (`ADMIN_EMAILS` in `lib/config.js`, kept equal to `firestore.rules`) |
| `expirePendingBookings` | Hourly | Frees unpaid holds past their 20-minute window |

## Data model (Firestore)

- `services/{id}` — `name`, `description`, `durationMinutes`, `amount` (halalas; `0` = free), `active`, `sortOrder`
- `availabilityRules/{id}` — `dayOfWeek` (0 = Sun), `startTime`, `endTime` (`HH:mm`). A day with no rules is a non-working day
- `blockedDates/{YYYY-MM-DD}` — holidays / time off
- `settings/general` — `meetLink` (static fallback Meet URL) and `breakMinutes` (gap kept between sessions, 0-120)
- `bookings/{id}` — snapshots the service name, duration, price and Meet link at booking time, so later edits never rewrite history. Status: `pending_payment` → `confirmed` / `expired`; `confirmed` → `cancelled`; `paid_slot_conflict` = paid but the time was lost, needs a refund (→ `refunded` once done). A booking also stores `calendarEventId`, `breakMinutes`, and once cancelled `cancelledBy`, `refundAmount`, `refundStatus` (`none` / `pending` / `refunded` / `needs_manual` / `refunded_manually`); a moved booking keeps `previousSlots` and `rescheduleCount`
- `rateLimits/{ip}` — abuse protection on `bookingRequest`

## The module as a whole

This repository is the reusable booking module: **`functions/`** (backend), **`admin/`** (the owner's panel),
**`frontend/`** (the customer booking and manage pages, themeable, with a script that copies them into any
website: see `frontend/README.md`), **`demo/`** (one-click local demo) and the tests. A new project starts from a tagged
commit of this repo, sets its own project id, admin email and site origin, and copies `frontend/` into its site.
The reusable payment adapters live in the separate shared module `Shared/payments-gateway-module`.

## Code layout

```
functions/
  index.js                 the functions above
  lib/availability.js      PURE slot generation + overlap logic (no I/O)
  lib/bookings.js          reservation / payment-confirmation / expiry (Firestore transactions)
  lib/services.js          service parsing + reading
  lib/settings.js          working hours, blocked dates, Meet link
  lib/confirmation.js      the one "booking just became confirmed" step, shared by free + paid paths
  lib/email.js             Resend emails (all interpolated values escaped)
  lib/refundPolicy.js      calculateRefundAmount() — today: full refund. The ONLY place cancellation rules will go
  lib/gateways/paymob.js   copy of Shared/payments-gateway-module's Paymob adapter
  lib/hmacUtil.js          copy of the shared HMAC helpers
  lib/rateLimit.js         per-IP limiter
  scripts/seedDefaults.js  one-time bootstrap (see below)
  test/                    plain-node tests + an in-memory Firestore stand-in
```

## Tests

```bash
cd functions && npm test     # ~290 checks, no emulator / credentials needed
```

Slot generation, overlap between different-length services, the paid-after-expiry
conflict case, service parsing, refund policy, email escaping and the seed script are
all covered. `bookings.test.js` was mutation-checked (breaking the overlap function
makes the expected checks fail).

## Deploying this version

1. `cd functions && npm test`
2. `firebase deploy --only functions` (from `booking-backend/`)
3. Seed the initial service + hours — either:
   - `node scripts/seedDefaults.js your-project-id` (needs credentials: `gcloud auth application-default login`, or a service-account key in `GOOGLE_APPLICATION_CREDENTIALS`). It only writes into **empty** collections, so it can never overwrite what the client later edits; **or**
   - create the documents by hand in the Firebase console; **or**
   - wait for the Phase 2 admin panel.
4. The seeded service uses a placeholder price (SAR 500) — set the real rate before going live.

## Cancel, reschedule and refunds

The confirmation email's "Manage my booking" link opens `Website-online-booking/pages/manage-booking.html` with
`?bookingId=...&token=...`. The 48-hex-character token is the only credential; every wrong id/token gets the same
generic 404, and repeated wrong guesses from one address are rate-limited.

- **Cancel** — only a `confirmed` booking whose session hasn't started. The refund amount comes from
  `calculateRefundAmount` (`lib/refundPolicy.js`, the ONLY place cancellation rules will go; today: full refund).
- **Reschedule** — same length, so no payment; respects working hours, blocked dates, the booking window and the
  break between sessions; a booking's own current time never blocks it; can be repeated.
- **Best-effort side effects** — the state change is a Firestore transaction; the Paymob refund, the calendar
  delete/move and the emails happen afterwards and never undo it. A failed refund sets `refundStatus: needs_manual`
  (flagged in the admin panel, owner emailed "ACTION NEEDED"); a failed calendar change emails the owner.
- **Unverified:** `PaymobGateway.refund` (`POST /api/acceptance/void_refund/refund`) is written from Paymob's docs, not
  tested against a real account. Try a real TEST-mode refund before relying on it; until then a failure just
  falls back to a manual refund.

## Admin panel

`admin/` is a static app (no build step) served by Firebase Hosting; it signs in with
Firebase Auth (email + password) and talks straight to Firestore. **`firestore.rules`
is the lock** — the panel's screens are only convenience. Tabs: Bookings (a quarter at a
time as three monthly calendars where each highlighted day shows how many sessions are booked, with the full
booking details below for the selected day, month or quarter; filter, mark no-show), Services (add/edit/pause/delete; price 0 = free), Schedule
(weekly hours with breaks, blocked dates, Meet link), Overview (headline numbers).
Upcoming confirmed bookings have a **Cancel booking** button (calls `adminCancelBooking`: refund, calendar,
emails). Cancellations whose automatic refund failed, and payments that lost their slot, show as **Needs
refund**; once the owner has refunded them in Paymob they press **Mark refunded**. The rules let an admin change
only `confirmed → no_show`, `refundStatus → refunded_manually`, and `paid_slot_conflict → refunded`, one field
each; cancelling is deliberately not a browser write. The admin CSP allows the Cloud Functions origin
(`connect-src` in `firebase.json`).

Setup (once):

1. Firebase console → Project settings → copy the **Web API key** into `admin/js/config.js`.
2. Put the real admin email(s) in the `isAdmin()` list in `firestore.rules` (replace the placeholder).
3. Firebase console → Authentication → enable Email/Password, **turn off sign-up**, and create the admin user(s) by hand. The panel makes them verify their email once.
4. `firebase deploy --only firestore:rules,hosting` (from `booking-backend/`).
5. Custom domain `admin.example.com`: Hosting → Add custom domain → add the CNAME/TXT it shows at the DNS host. Optionally restrict the Web API key to that domain in Google Cloud → Credentials.

Tests: `cd admin && node test/lib.test.js` (pure logic). Rules tests:
`cd rules-test && npm test` (against the Firestore emulator; needs Java 21+ on
PATH). Local emulator ports (auth 9199, firestore 8180, hosting 5100, functions 5101,
pubsub 8185) avoid the common 8080 clash. The Hosting emulator ignores `headers`, so the
CSP was checked with a small server applying the `firebase.json` rules instead.

## Local demo: fake checkout and mailbox

Before Paymob and Resend accounts exist, the whole paid journey can be shown locally. Inside the Firebase
emulator only (`FUNCTIONS_EMULATOR`, which a deployed function never has):

- with the placeholder Paymob key, a paid booking sends the customer to `demo/checkout.html` (Pay / Decline)
  instead of Paymob. **Pay** runs the same `handlePaymentEvent` the real webhook worker uses
  (`lib/payments.js`), so confirmation, calendar and email behave identically;
- with the placeholder Resend key, emails are stored in `demoOutbox` and shown by `demo/mailbox.html`;
- `demoCompletePayment` and `demoMailbox` exist only in the emulator; `lib/gateways/mock.js` refuses to
  construct anywhere else.

**To run it: double-click `demo/start-demo.bat`** (or `node demo/run-demo.js`). It starts the emulators, loads demo
data (dates are relative to today), serves the site and admin from copies in `demo/.build` (the real files are never
edited) and opens the browser. Leave the window open while presenting; close it or press Ctrl+C to stop
(`demo/stop-demo.bat` cleans up if it was closed abruptly). Needs Node, the Firebase CLI and a Java 21 runtime in
`demo/.tools/jre` (git-ignored; `bin/java.exe` must exist) or on PATH. Everything listens on `127.0.0.1` only.
The admin sign-in is `owner@example.com` / `demo-password-1` (exists only in the local Auth emulator). A real
Paymob/Resend key in the emulator switches that part back to the real service.

Real checkout returns the customer to `book-a-session.html?bookingId=...`; the page then polls `bookingStatus`
(id-only, no personal data) until the webhook has confirmed, and shows "You're booked" or the right message.

## Before trusting this with real payments

`lib/gateways/paymob.js` was written from Paymob's published documentation, not a
tested call against a real account. Verify with a real TEST-mode transaction:
the HMAC matches, `obj.order.merchant_order_id` really echoes our `special_reference`,
and the Create Intention request/response shape. Refunds (Phase 3) use Paymob's
*older* "Accept" API with a separate auth-token step — also unverified.

## Still placeholder / open

- Secrets: `PAYMOB_SECRET_KEY`, `PAYMOB_PUBLIC_KEY`, `PAYMOB_HMAC_SECRET`, `RESEND_API_KEY` are placeholder text (`firebase functions:secrets:set <NAME>` to replace; `PAYMOB_WEBHOOK_SHARED_SECRET` is a value you invent and also paste into Paymob's webhook header `X-Webhook-Secret`).
- Paymob `paymentMethods` (integration IDs) — passed to `new PaymobGateway({...})` in `bookingRequest`.
- `ALLOWED_ORIGINS` in `lib/config.js` still includes the localhost dev origin — remove before going live.
- `example.com` Google Workspace status (decides how a per-booking Meet/Calendar integration would work later).
- Rate limiter trusts the `x-forwarded-for` header; confirm on real infrastructure that it can't be spoofed to dodge the limit.

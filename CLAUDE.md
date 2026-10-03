# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this folder is

Workspace for turning an existing single-client booking engine into a multi-tenant booking SaaS. **Status: architecture draft only — no multi-tenant code written yet.**

- `ARCHITECTURE.md` — the SaaS design draft. Key decision already made: **automated per-tenant provisioning** (one Firebase project per tenant, driven by a control plane), *not* a shared `businessId`-scoped database. Firebase is the working assumption but explicitly open to revisit. Read its "Open questions" section before proposing design changes.
- `schedule-booking/` — the only code. Proven single-tenant engine, copied from the original client project. That project is separate: never sync or copy between it and this repo. `schedule-booking/SAAS_README.md` is the gap analysis (everything that's hardcoded per client today).
- **Direction (approved by the founders 2026-09-30):** a standalone AI-powered scheduling, productivity and business-management SaaS, with the original client as an early pilot customer. Roles: CEO/CFO (product, pricing, go-to-market), CTO (architecture, engineering), COO (operations, delivery, marketing). Only the direction and roles are agreed; product name, customer segments, first-release scope, architecture, payment provider and pricing are still open. Treat `ARCHITECTURE.md` as input for the CTO — it is not a decision. See `README.md`.
- **No client details or personal names in this repo.** Use neutral placeholders (`owner@example.com`, `example.com`, `your-project-id`) and roles instead of names. The pilot customer's identity, founder names and founder decisions live in the private project tracker only. Do not put tracker issue ids, project names or document titles in this repo.
- The project tracker holds the technical definition and the competitor research. The CTO has asked that Claude own the documentation as the single source of truth, and the README principle is to keep "what exists / what is being evaluated / what has been proposed / what has been agreed" clearly apart and not duplicate founder decisions in the repo. The CTO's architecture documents describe a **different design** (serverless API + PostgreSQL + Pydantic, one logical platform with tenant-scoped data); `ARCHITECTURE.md` compares the two. Don't present the Firebase draft as the agreed architecture.
- Payment provider is unresolved: engine and CTO blueprint use Paymob; the competitor research assumes Paylink.sa.

## Git

This whole folder is one git repo, pushed to the public GitHub repo `gritnations/SaaS` (`main`). History in this repo starts at the baseline commit; the previous repository's history was not carried over. Commit identity is the GitHub noreply email. The GitHub CLI is installed at `D:\Tools\GitHub CLI\bin\gh.exe` (on the user PATH; in Git Bash add `/d/Tools/GitHub CLI/bin` to `PATH` if `gh` isn't found).

## Commands (run from `schedule-booking/`)

```bash
cd functions && npm install && npm test   # ~290 checks, plain node, no emulator/credentials
node functions/test/bookings.test.js      # run a single test file (each is standalone)
cd admin && node test/lib.test.js         # admin pure-logic tests
cd rules-test && npm test                 # firestore.rules tests via emulator (needs Firebase CLI + Java 21+)
node demo/run-demo.js                     # full local demo (emulators + fake Paymob checkout + fake mailbox); --stop to clean up
node frontend/sync-to-site.js <site> --with-pages   # copy customer frontend into a website folder
```

No build step, no linter. Tests use a hand-rolled `check(name, cond)` harness; Firestore logic is tested against the in-memory stand-in `functions/test/fakeFirestore.js` (models transactions' all-or-nothing writes, not concurrency). New test files must be added to the `test` script chain in `functions/package.json`.

Note: `demo/run-demo.js` expects the customer site at `../Website-online-booking` (sibling of `schedule-booking/`), which is **not** present in this SaaS workspace — the demo won't fully run here without it. `.firebaserc` is gitignored and absent.

## Architecture (booking engine)

Three deployable parts in one Firebase project, no framework anywhere:

- **`functions/`** — Cloud Functions v2, Node 22, CommonJS, `us-central1`. `index.js` is thin HTTP/PubSub/scheduler wiring; logic lives in `lib/`. `lib/availability.js` is pure (no I/O) slot/overlap math; `lib/bookings.js` does reservations/expiry in Firestore transactions; `lib/confirmation.js` is the single "booking became confirmed" step shared by the free and paid paths; `lib/refundPolicy.js` is the *only* place cancellation rules may go.
- **`admin/`** — static ES-module SPA on Firebase Hosting, Firebase Auth email/password, reads/writes Firestore directly. **`firestore.rules` is the real authorization boundary**; the UI is convenience. Rules deliberately allow admins only narrow single-field transitions (`confirmed→no_show`, `refundStatus→refunded_manually`, `paid_slot_conflict→refunded`); cancel/refund must go through the `adminCancelBooking` function, never a browser write.
- **`frontend/`** — customer booking + manage pages, themeable via `--booking-*` CSS vars. Source of truth; sites get copies via `sync-to-site.js`. Elements found by `id`; all text inserted via `textContent`, never HTML.

Payment flow: `bookingRequest` → Paymob intention/checkout → `paymobWebhookIngest` (verifies HMAC + `X-Webhook-Secret`, holds only those secrets) → Pub/Sub → `processPaymobEvent` → `lib/payments.js#handlePaymentEvent`. Service `amount: 0` skips payment and confirms instantly. Paid holds expire after 20 min (`expirePendingBookings`). A payment arriving for a slot taken meanwhile becomes `paid_slot_conflict` (refund needed), never a double-booking. Side effects (refund, calendar, email) run after the transaction and never roll it back — failures set `refundStatus: needs_manual` / email the owner.

Customer manage links use a 48-hex token as the only credential; all wrong id/token combos return the same generic 404.

Demo mode (`lib/gateways/mock.js`, `demoOutbox`, `demoCompletePayment`/`demoMailbox`) only activates inside the emulator (`FUNCTIONS_EMULATOR`) with placeholder keys; the mock gateway refuses to construct elsewhere. Emulator ports: auth 9199, firestore 8180, pubsub 8185, hosting 5100, functions 5101.

`lib/gateways/paymob.js` and `lib/hmacUtil.js` are **copies** of `Shared/payments-gateway-module` — the shared module is the upstream. Paymob calls (incl. refunds) are written from docs and unverified against a real account.

## Per-tenant values currently hardcoded (the SaaS seam)

These are what the control plane would need to inject; they must stay in sync by hand today:

- `functions/lib/config.js` — `ADMIN_EMAILS`, `ADMIN_ORIGINS`, `ALLOWED_ORIGINS`, `PUBLIC_SITE_ORIGIN`, `CALENDAR_OWNER_EMAIL`, `CURRENCY` (SAR, amounts in halalas), `TIMEZONE`/`TIMEZONE_OFFSET` (Asia/Riyadh, fixed +03:00).
- `firestore.rules` `isAdmin()` email list — **must equal `ADMIN_EMAILS`**.
- `admin/js/config.js` — Firebase web config, `functionsBase`, `CURRENCY` (must match functions).
- `firebase.json` CSP `connect-src` — contains the project's Cloud Functions origin.
- Secrets via `defineSecret` (Secret Manager): `PAYMOB_SECRET_KEY`, `PAYMOB_PUBLIC_KEY`, `PAYMOB_HMAC_SECRET`, `PAYMOB_WEBHOOK_SHARED_SECRET`, `RESEND_API_KEY`, `GOOGLE_CALENDAR_KEY`.

Business data (services, weekly hours, blocked dates, Meet link, break minutes) is already in Firestore, not code — see the data model in `schedule-booking/README.md`.

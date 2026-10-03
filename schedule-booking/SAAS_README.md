# Schedule Booking — SaaS Conversion Notes

This folder is a copy of a booking backend first built for a single
client, brought over as the starting
point for turning it into a general-purpose, multi-tenant booking SaaS.
Copied into this repository without its commit history, and without any of
the client's real secrets, `node_modules`, or local build artifacts — those
were gitignored and never committed.

The issues for the original client deployment itself are tracked
separately. This document is about the separate question of productising
the same engine for multiple clients.

**Update 2026-10-01:** the founders approved (2026-09-30) making this a
standalone AI-powered SaaS with the original client as an early pilot
customer. The work is tracked in the project tracker. The CTO's
architecture documents propose a PostgreSQL-based platform rather than this
Firebase engine. The current SaaS architecture draft and its comparison
with that blueprint are in the parent folder: `../ARCHITECTURE.md`,
`../README.md`.

## What's built (proven, working engine)

A Firebase Cloud Functions (2nd gen, Node 22) + Firestore backend for
appointment booking, built and tested locally end to end:

- **Multi-service booking** — each service has its own duration and price;
  a service priced at 0 confirms instantly with no payment step, so the
  same code serves both paid and free-scheduling use cases
- **Firestore-driven availability** — weekly hours, blocked dates, and a
  configurable break between sessions; overlap-safe reservation via
  Firestore transactions (mutation-tested, not just happy-path tested)
- **Payments** — Paymob integration (intention → checkout → HMAC-verified
  webhook → Pub/Sub → confirmation), with a hold/expiry window for unpaid
  reservations and automatic conflict handling if a paid slot is lost in
  the meantime
- **Cancel / reschedule** — customer-facing, secured by a private
  token link (not a login); refund amount is policy-driven
  (`lib/refundPolicy.js`); reschedule respects hours/blocks/breaks and can
  be repeated
- **Admin panel** (`admin/`) — static app, Firebase Auth login, talks
  directly to Firestore with `firestore.rules` as the real authorization
  boundary. Tabs: Bookings (calendar view, no-show marking), Services,
  Schedule (hours/breaks/blocked dates), Overview. Handles the cases
  automation can't: manual cancel with refund, flagging refunds that need
  a human follow-up
- **Email** — transactional confirmation/cancellation/reschedule emails
  via Resend, all interpolated values escaped
- **Local demo mode** — the entire paid journey (fake Paymob checkout,
  fake mailbox) runs in the Firebase emulator with zero real credentials,
  useful for sales demos and onboarding
- **Test coverage** — ~290 checks across booking logic, availability,
  refund policy, email escaping, and Firestore security rules (emulator-
  based), no live credentials needed to run

Full detail on any of the above: `README.md` in this folder (carried over
from the original).

## Why this isn't SaaS yet

The current design is a **per-client template**, not shared multi-tenant
software. The original README says this outright: *"A new project starts
from a tagged commit of this repo, sets its own project id, admin email
and site origin, and copies `frontend/` into its site."* That's
fork-and-redeploy, not one backend serving many businesses. Concretely:

- **One Firebase project per client** — the target project id is hardcoded
  in `.firebaserc` (currently `your-project-id`). Each client needs
  their own Firebase project, their own deploy, their own bill.
- **Tenant identity lives in code, not data** — `functions/lib/config.js`
  hardcodes `ADMIN_EMAILS`, `ADMIN_ORIGINS`, `PUBLIC_SITE_ORIGIN`,
  `CALENDAR_OWNER_EMAIL`, `CURRENCY` (one currency per deployment), and
  `TIMEZONE` (fixed to `Asia/Riyadh`). Onboarding a new client today means
  editing source and redeploying, not filling in a form.
- **No tenant field anywhere in Firestore** — `services/{id}`,
  `bookings/{id}`, `availabilityRules/{id}`, `blockedDates/{date}`,
  `settings/general` are all flat collections scoped to one project. A
  shared multi-tenant database would need a `businessId` (or equivalent)
  on every document, plus rules and queries updated to scope by it.
  Alternative: keep one Firebase project per tenant but automate
  provisioning — avoids a rules/query rewrite, trades it for
  infrastructure-as-code and per-tenant billing plumbing.
- **Single admin allowlist** — `ADMIN_EMAILS` is one flat array checked in
  both `lib/config.js` and `firestore.rules` (the two must be kept in
  sync by hand). A SaaS product needs per-tenant admin accounts/roles,
  not a global list.
- **Payment gateway is Saudi-specific** — Paymob + SAR only. Serving
  clients outside that market means a gateway abstraction and
  multi-currency support (the module's own docs flag this: the reusable
  adapter already lives separately in `Shared/payments-gateway-module`,
  which is a reasonable seam to build on).
- **No SaaS-layer concerns exist yet** — no signup/onboarding flow, no
  billing/subscription/metering, no per-tenant custom domain automation
  (today's custom-domain setup is a manual Firebase Hosting + DNS step per
  client), no tenant-level usage limits or plan tiers.
- **Admin panel is single-tenant UI** — it assumes it's talking to one
  Firestore database for one business; a SaaS admin experience would need
  either a tenant switcher or fully separate deployments per tenant (same
  fork trade-off as above, one level up the stack).

## Open question for the SaaS design session

The core fork-in-the-road: **shared multi-tenant backend** (one Firebase
project, `businessId`-scoped Firestore, rewritten rules/queries, real
tenant isolation risk to get right) vs. **automated per-tenant
provisioning** (keep today's one-project-per-client isolation, but script
the fork/deploy/config steps that are currently manual). The second is
less risky and closer to what's already proven; the first is the more
conventional SaaS shape and scales operationally better past a handful of
clients. Worth deciding this before touching code.

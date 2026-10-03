# Schedule Booking SaaS — Architecture Draft

Draft architecture for turning an existing single-client booking backend
(`schedule-booking/`, copied from the original client project)
into a multi-tenant SaaS product. See `schedule-booking/SAAS_README.md` for
what's already built and the full gap analysis this draft builds on.

**Status: draft for CTO review, not yet built, not agreed.** Updated
2026-10-01.

## Context: approved direction and the CTO's blueprint

**Founders' alignment** (**approved 2026-09-30**; details and roles are
kept in the project tracker, not in this repository):

- The product becomes a standalone **AI-powered scheduling, productivity
  and business-management SaaS**. Scheduling and payments are the
  foundation; tasks, customer workflows and AI assistance may follow,
  subject to customer validation. None of these later features are
  committed.
- **The original client deployment is the first pilot customer**, not the
  only one the product serves.
- Roles: CEO & CFO (product vision, UX, pricing, go-to-market), CTO
  (architecture, engineering, integrations, security), COO (operations,
  delivery, marketing, launch). **Architecture decisions sit with the
  CTO**; this document is input to that decision, not a decision.
- The approved working principles keep existing technical architecture and
  implementation work as it is unless separately reviewed and agreed. The
  technical definition is a tracker issue that defines the scheduling tool
  (serverless API, PostgreSQL, Pydantic), which the CTO's documents below
  expand on.

**The CTO's architecture documents** (created 2026-09-28/29): an
architecture blueprint, an architecture diagram and an executive brief,
all describing one logical scheduling and payment platform. They describe
a **different design** from this draft:

| | CTO's blueprint | This draft |
|---|---|---|
| Starting point | New build: serverless API + PostgreSQL + Pydantic contracts, with the existing website as a thin client | The existing, tested Firebase booking engine in `schedule-booking/` |
| Data store | PostgreSQL, one logical platform | Firestore, one Firebase project per business |
| Separating businesses | One platform; add tenant-scoped data and permissions later, without building a large multi-tenant SaaS too early | Separate project per business, created automatically by a control plane |
| Payments | Paymob KSA (Mada) behind a provider-neutral adapter, signed/HMAC webhooks | Same: Paymob behind an adapter, HMAC-verified webhook |
| Admin | Explicit, audited admin authority; no generic table editing | Admin panel limited by Firestore rules; cancel/refund only through a server function |
| Sequence | Staged preliminary tasks (inventory, domain spike, concurrency/schema, payment boundary, security, release plan) before any implementation | Reuse the engine; add control plane, sign-up and billing |

**Where they already agree:** Paymob as the Saudi gateway, payment kept
behind an adapter, the server (never the browser) deciding price and
slot ownership, webhooks as the only proof of payment, and explicit
handling of holds, expiry, late payments and refunds.

**What the existing engine can offer the blueprint either way:** it already
implements and tests many of the blueprint's failure cases — overlap-safe
reservation in a transaction, 20-minute holds with hourly expiry, a paid
booking whose slot was lost (`paid_slot_conflict`), refunds that fail
falling back to manual, and token-secured cancel/reschedule (~290 checks).
Even if the platform moves to PostgreSQL, its logic and tests are a
reference for the concurrency and schema work.

**Decision needed from the CTO:** build on the Firebase engine (this draft)
or on the PostgreSQL blueprint, and whether businesses are separated by
project or by tenant-scoped data. The rest of this document describes the
Firebase option.

**Platform note:** this draft assumes Firebase (Functions + Firestore +
Hosting), matching the existing proven code. That's provisional — there's
an open possibility of moving off Firebase for the SaaS version. Keeping
Firebase as the working assumption until an alternative is decided, so
this doesn't block on it.

## Core decision: per-tenant provisioning, not shared multi-tenant database

Two ways to isolate tenants were on the table:

1. **Shared multi-tenant backend** — one Firebase project, every Firestore
   document scoped by a `businessId`, rules and queries rewritten to filter
   by it. More conventional SaaS shape, scales operationally better past a
   handful of clients, but means rewriting the data model and re-proving
   isolation is airtight.
2. **Automated per-tenant provisioning** — keep today's one-Firebase-
   project-per-client isolation (already proven, already tested), but
   replace the manual fork/deploy/config steps with an automated control
   plane that does it on signup.

**This draft goes with option 2.** It builds directly on the working code
instead of rewriting it, and tenant isolation is Firebase-project-level
rather than something a rules bug could quietly break. Revisit if the
tenant count grows enough that per-project operational overhead (billing,
monitoring, quota management across N Firebase projects) becomes the
bigger cost.

## System architecture

```
                    ┌──────────────────┐
                    │  Platform admin  │
                    └────────┬─────────┘
                             │
                             ▼
        ┌───────────────────────────────────────┐
        │              Control plane             │
        │  tenant registry · billing ·           │
        │  provisioning · integrations           │
        └───────────┬─────────────────┬───────────┘
                     │                 │
                     ▼                 ▼
        ┌─────────────────────┐ ┌─────────────────────┐
        │ Tenant: business A  │ │ Tenant: business B   │
        │ Firebase project    │ │ Firebase project     │
        │ Functions +         │ │ Functions +          │
        │ Firestore + Hosting │ │ Firestore + Hosting  │
        │ + admin panel       │ │ + admin panel        │
        │ + public API        │ │ + public API         │
        └──────────┬──────────┘ └──────────┬───────────┘
                    │                       │
                    └───────────┬───────────┘
                                 ▼
              ┌───────────────────────────────────┐
              │   Shared modules & integrations    │
              │ payments · email · SMS ·           │
              │ WhatsApp · calendar (pluggable,    │
              │ activated per tenant)              │
              └───────────────────────────────────┘
```

Each tenant is a full copy of the existing `functions/` + `admin/` +
`frontend/` stack, deployed into its own Firebase project by the control
plane instead of by hand. The payment gateway adapter
(`Shared/payments-gateway-module`), email, and the new communication
channel adapters (SMS, WhatsApp, calendar) stay as shared, versioned
modules pulled into each tenant deploy — not duplicated per tenant. Each
tenant's Functions also expose a public, API-key-secured booking API, so a
client can integrate booking directly into their own website instead of
using the bundled templates.

### What the control plane manages, per tenant

Everything that's currently hardcoded in `functions/lib/config.js` and
`.firebaserc` for the one original client deployment needs to become data the
control plane owns and injects at provision time:

**Identity & routing**
- Tenant record (business name, plan, status: provisioning / active /
  suspended / cancelled)
- Firebase project ID mapping (which project this tenant's data lives in)
- Custom domain + subdomain mapping (`admin.{tenant}.com`, and the
  customer-facing booking domain)

**Tenant-specific config** (today hardcoded in `config.js`)
- Admin email(s) / owner account
- Public site origin (`PUBLIC_SITE_ORIGIN`) and allowed CORS origins
- Currency and timezone
- Calendar owner email (for Meet/Calendar integration)

**Credentials, scoped per tenant**
- Paymob keys + webhook shared secret + integration IDs
- Public API key(s), issued per tenant for their own website integration
- Resend API key (or a shared sending account with a per-tenant sender
  identity)
- Google Calendar service-account key, if used

**Integrations**
- Which communication channels a tenant has connected (email provider,
  SMS, WhatsApp, calendar, ...) and their per-channel credentials/
  connection state
- Which channels are activated for which booking events (confirmation,
  reminder, cancellation)

**Billing**
- Subscription plan/tier and payment method for the tenant's own SaaS bill
  (separate from Paymob, which handles the tenant's *customers'* payments)
- Usage metering, if plans end up usage-based (e.g. bookings/month)

**Lifecycle actions**
- Trigger provisioning (create Firebase project or tenant record, deploy
  functions/rules, seed defaults) on signup
- Suspend/reactivate a tenant (e.g. on non-payment)
- Deprovision on cancellation

The admin panel and `firestore.rules` need to start reading tenant admin
identity from this instead of the static `ADMIN_EMAILS` array — today that
array has to be hand-edited and kept in sync between `config.js` and
`firestore.rules` for every client.

## Client-facing capabilities

What a subscribing business gets, and how it maps onto the architecture
above:

1. **Integrate via API** — each tenant's Firebase project already exposes
   the booking API (`listServices`, `availableSlots`, `bookingRequest`,
   etc.). For SaaS this becomes a public, documented, per-tenant API
   secured by an API key instead of origin-allowlisting alone — lets a
   client call it straight from their own website instead of using the
   bundled templates.
2. **Manage bookings natively via their account** — the existing admin
   panel, unchanged in shape, backed by control-plane-issued tenant
   accounts instead of one hardcoded admin email.
3. **Connect communication tools of their choice** — new: an integrations
   layer alongside the payment adapter, inside "Shared modules &
   integrations" (email, SMS, WhatsApp, calendar as pluggable adapters).
   A tenant connects and activates whichever channels they want from
   their account; the control plane stores each tenant's per-channel
   connection state and credentials.
4. **Turn payments on or off** — already built. A service priced at 0
   confirms free; pricing it engages Paymob. At the tenant level this
   becomes: connect a payment gateway, or leave it disconnected and only
   run free bookings.

## Tenant lifecycle workflow (business owner)

```
Sign up (business owner)
   │
   ▼
Provision tenant (platform, automated)
   │
   ▼
Configure business (business owner)
   │
   ▼
Go live (platform)
   │
   ▼
Operate: bookings + billing  ─┐
   ▲                          │
   └── ↻ ongoing: new bookings, support, plan changes
```

- **Sign up** — business owner registers, picks a plan
- **Provision tenant** — control plane creates the Firebase project (or
  tenant record), deploys functions + rules, seeds default settings —
  automated, no manual fork/deploy step
- **Configure business** — services, prices, weekly hours, branding,
  payment gateway connection, custom domain
- **Go live** — customer-facing booking page goes active on the tenant's
  domain
- **Operate** — ongoing loop: customers book, owner manages via the admin
  panel, platform bills the tenant on their plan

**Not yet decided:** exactly what "configure business" collects at
signup, plan tiers and pricing, and how custom domain verification gets
automated (today it's a manual Firebase Hosting + DNS step per client).

## Customer booking sub-flow

```
Browse services (customer)
   │
   ▼
Pick a slot (live availability check)
   │
   ▼
Pay or confirm (skipped if free service)
   │
   ▼
Booking confirmed (email + calendar invite)
   │
   ▼
Manage booking (cancel or reschedule via link)
```

This is the existing, proven customer journey from the original client
build — carries over unchanged into the SaaS version. No account needed;
a private token link is the only credential for managing a booking.

## AI capabilities (not yet designed)

The approved direction calls the product "AI-powered" and asks which AI
feature gives enough customer value to include in the first release.
Candidates it names: scheduling assistance, meeting summaries, task
creation, reminders and workflow recommendations. Competitors already sell
AI add-ons (Calendly's AI meeting tools, Acuity's AI Booking Assistant on
Premium, SimplyBook's AI voice booking), so this is becoming expected.

Nothing in this draft supports AI yet. Architecture points to settle once a
first feature is chosen:

- Where AI calls run (a server function per business, or a shared service)
  and how a business's data is kept to its own requests
- What customer data may be sent to an AI provider, and consent for it
- Cost per business, since AI calls are paid per use and must fit the plan
  prices

## Integration rule (from the CTO)

For any payment, messaging or calendar integration, the source of truth is
our own documentation checked against the provider's **latest published
documentation**, every time, however recently it was last checked.
Grouping providers by how similar their APIs are is a useful shortcut for
finding targets, but never replaces that check.

## Open questions for the next pass

- **Firebase engine vs. PostgreSQL blueprint** (see "Context" above) — for
  the CTO to decide
- **Payment provider:** the engine and the CTO's blueprint use Paymob, and
  the CTO has made a KSA/MENA processor with Mada an absolute condition
  (Paymob proposed). But the original tool definition still specifies
  Stripe Checkout, and the competitor research assumes Paylink.sa. Agree
  which one, or whether the adapter supports several
- Which AI feature, if any, goes into the first release
- Plan tiers, pricing, and how usage (if any) is metered — the decision
  is the CEO's
- What signup/configuration data is collected before a tenant can go live
- How custom domain verification gets automated per tenant
- Whether each communication channel (email, SMS, WhatsApp, calendar) is
  one shared account per channel (per-tenant sender identity) or fully
  separate credentials per tenant
- Which communication channels to launch with first — not decided
- Multi-currency / non-Saudi-market support, given Paymob is SAR-only
  today

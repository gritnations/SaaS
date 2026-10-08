# SaaS

Working repository for the standalone AI-powered scheduling, productivity and business-management SaaS.

The project is in the **platform foundation stage**. A Python platform spine now exists. The existing booking engine is a reference implementation and an existing system. The multi-tenant SaaS has not been built.

**Status:** platform spine only; no booking domain, tenancy or live payments on it
**Last updated:** 2026-10-08

## Purpose

The aim is to evaluate and develop the existing booking system into a standalone SaaS that can support multiple independent businesses.

Scheduling and payments are the initial product foundation. Additional capabilities such as customer workflows, task management and AI assistance may be considered later, subject to customer validation and product decisions.

The existing booking engine is a reference implementation and the pilot's existing system. It is not the SaaS architecture and is not being migrated.

## What has been agreed

The founders approved the project direction on 2026-09-30:

* the product is a standalone AI-powered scheduling, productivity and business-management SaaS, with scheduling and payments as the initial foundation;
* the original client is an early pilot customer, not the sole scope;
* founder roles are defined (CEO & CFO, CTO, COO);
* existing technical architecture and implementation work stays as it is unless separately reviewed and agreed.

The charter, roles and workstreams live in the project tracker and are not duplicated here. Everything listed under "Product status" below is still open.

## Current state

* **Booking engine:** existing Firebase Cloud Functions + Firestore application with admin and customer interfaces and local demo support.
* **Testing:** the existing engine has been tested locally; production deployment and live payment/email integrations remain outstanding.
* **Python platform spine:** `main.py` → `api/main.py` → `postgres.py`, with health, readiness and metrics routes, a database boundary and a provider-neutral payment seam. Tested without a database. It has not been run against a real PostgreSQL server.
* **SaaS on the platform:** not yet implemented. There is no booking domain on the platform.
* **Multi-tenancy:** not yet implemented. The tenancy model is not decided.
* **Architecture decision:** decided by the CTO. The platform is Python, an API and PostgreSQL. The Firebase engine stays as existing reference code.
* **Payments:** the platform's payment boundary is provider-neutral. Paymob is the first provider and is not live. A secondary provider is a stub.
* **Product scope:** first-release scope, target customer segments, product positioning and pricing remain subject to agreement.

Nothing beyond the agreed first-release scope should be treated as committed product functionality.

## Repository contents

### Python platform (repository root)

`main.py` is the ingress and security boundary. `api/` holds the FastAPI application and its routes. `postgres.py` is the database boundary. `payments/` is the payment boundary: one interface, a Paymob adapter that is not live, and a stub. `tests/` holds the tests. Commands are in `CLAUDE.md`.

**Status:** foundation only.

### `ARCHITECTURE.md`

States the current platform direction, then keeps the earlier Firebase draft as a record of the alternative that was considered.

**Status:** the direction is decided; the Firebase draft in it is not the platform architecture.

### `schedule-booking/`

The existing booking engine from which the SaaS work originated.

It currently contains:

* Firebase Cloud Functions
* Firestore
* administration functionality
* customer-facing booking functionality
* local demonstration support

See [`schedule-booking/README.md`](schedule-booking/README.md) for the existing system and [`schedule-booking/SAAS_README.md`](schedule-booking/SAAS_README.md) for the current gap between the booking engine and a multi-tenant SaaS.

## Architecture status

The platform direction is decided by the CTO: Python, an API and PostgreSQL, with the security boundary on the server.

Two approaches were evaluated:

1. a Firebase-based architecture building on the existing booking engine; and
2. a PostgreSQL-based architecture designed around the requirements of a multi-tenant SaaS.

The second was chosen. The tenancy model, the booking domain schema, and hosting and runtime are not decided.

## Product status

This repository should be treated as **pre-productisation**.

The following remain open decisions:

* product name
* initial customer segments
* first-release feature set
* SaaS tenancy model
* hosting and runtime infrastructure
* payment provider
* pricing
* product positioning
* longer-term AI and business-management functionality

Until these decisions are agreed, documents in this repository should be read as research, proposals or working drafts rather than specifications of committed functionality.

## Working principle

Keep the distinction clear between:

* **what already exists**
* **what is being evaluated**
* **what has been proposed**
* **what has been agreed**

The README describes the state of the repository. Detailed strategic decisions, founder discussions and implementation decisions should live in the appropriate project documentation rather than being duplicated here.

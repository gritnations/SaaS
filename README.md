# SaaS

Working repository for the standalone AI-powered scheduling, productivity and business-management SaaS.

The project is currently in the **research and architecture stage**. The existing booking engine provides the starting implementation; the multi-tenant SaaS architecture has not yet been implemented.

**Status:** research and architecture drafts
**Last updated:** 2026-10-01

## Purpose

The aim is to evaluate and develop the existing booking system into a standalone SaaS that can support multiple independent businesses.

Scheduling and payments are the initial product foundation. Additional capabilities such as customer workflows, task management and AI assistance may be considered later, subject to customer validation and product decisions.

The existing booking engine is being treated as the initial technical foundation and pilot implementation, rather than as the final SaaS architecture.

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
* **SaaS architecture:** not yet implemented.
* **Multi-tenancy:** not yet implemented.
* **Architecture decision:** Firebase and PostgreSQL approaches are currently being evaluated.
* **Payments:** the payment-provider choice for the SaaS has not yet been finalised.
* **Product scope:** first-release scope, target customer segments, product positioning and pricing remain subject to agreement.

Nothing beyond the agreed first-release scope should be treated as committed product functionality.

## Repository contents

### `ARCHITECTURE.md`

Current architecture draft for the SaaS.

It documents the proposed control-plane and business-isolation approach, compares the Firebase approach with the alternative PostgreSQL architecture, and records open questions around AI, payments and tenancy.

**Status:** draft; not yet the final architecture.

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

The architecture is deliberately not treated as settled.

Two approaches are currently being evaluated:

1. a Firebase-based architecture building on the existing booking engine; and
2. a PostgreSQL-based architecture designed around the requirements of a multi-tenant SaaS.

The final choice should follow from the product, tenancy, security, operational and integration requirements rather than from the implementation of the existing prototype alone.

## Product status

This repository should be treated as **pre-productisation**.

The following remain open decisions:

* product name
* initial customer segments
* first-release feature set
* SaaS tenancy model
* architecture and infrastructure
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

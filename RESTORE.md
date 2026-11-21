# Database backup and restore runbook

**Status:** rehearsed once, on 17 November 2026, against a local PostgreSQL 17 server, using the staging database's schema and data. It says what was proven, how to repeat it, and what was not covered.

**State described:** database migrations 0001 to 0019; the hosted database is a Supabase project on the Free plan, which has no platform backups. Nothing here describes a production system, because none exists yet.

## 1. What was proven

- **Rebuild from the repository.** An empty database built from migrations 0001 to 0019 had all 19 applied, 9 tables, and all 19 files listed in the migration ledger. About 5 seconds.
- **Tests on the rebuilt database.** The full suite with `BOOKING_TEST_DATABASE_URL` set: 619 passed, 2 failed, 1 skipped. Both failures were out-of-date tests, since fixed (see section 5).
- **Backup.** A read-only dump of the staging `public` schema: 48 KB, exit 0, about 8 seconds.
- **Restore.** The dump restored into a fresh database with no errors, in about 1.4 seconds.
- **Comparison with staging.** The restored copy matched: row counts in all 9 tables, 90 constraints, 26 indexes, the grants of `app_api`, `postgres` and `service_role`, row security on all 9 tables, and the latest booking.
- **Tests on the restored copy.** 618 passed, 3 failed: the same 2, plus 1 test that assumes an empty table (see section 5).
- **The API on the restored copy, connected as `app_api`.** `/health` 200, `/health/ready` 200, the services and availability routes 200.
- **Database down.** `/health` stayed 200, `/health/ready` answered 503 `not_ready`, and the booking route answered 503 "Booking is unavailable." after about 3.2 seconds. The log held no secrets and no tracebacks.
- **Database back.** The API was ready again in about 0.4 seconds, with no restart.

Hands-on time for a full restore with checks: about 5 minutes. The staging data is tiny; times will grow with real data.

## 2. Take a backup

When: before every migration, and weekly, until a paid backup plan is decided.

```bash
# DATABASE_URL must be the database owner's connection string. Never print it or commit it.
PGOPTIONS="-c default_transaction_read_only=on" \
  pg_dump -d "$DATABASE_URL" -Fc -n public -f "staging-public-$(date +%Y%m%d-%H%M).dump"
```

- Use `pg_dump` from PostgreSQL 17 or later, to match the server.
- Keep dump files outside the repository and off shared drives. They hold customer names, emails and phone numbers (test data today, real data after launch).

## 3. Restore

Into a fresh database (local, or a new Supabase project):

1. Create the empty database.
2. Drop its empty `public` schema. PostgreSQL 15 and later create one, and the dump creates its own.
3. Create the extension the double-booking guard needs. Supabase keeps it outside `public`, so the dump does not include it:

   ```sql
   create schema if not exists extensions;
   create extension btree_gist schema extensions;
   ```

4. Make sure these roles exist: `app_api`, `anon`, `authenticated`, `service_role`, `supabase_admin`. A new Supabase project already has all of them except `app_api`.
5. Restore: `pg_restore -d <target> --exit-on-error <file>.dump`.
6. Set a new `app_api` password (`ALTER ROLE app_api PASSWORD ...`). Backups never contain role passwords. Keep it outside the repository and put the matching `DATABASE_URL` in the hosting settings, then redeploy.
7. Check: row counts, constraint and index counts, grants, then `/health/ready` and one booking-page request.

To check the repository's migrations alone, skip steps 2 to 6: apply `migrations/0001` to `0019` in order to an empty database, then run the test suite with `BOOKING_TEST_DATABASE_URL` pointing at it.

## 4. Not covered

- **Owner sign-in accounts.** They live in Supabase's `auth` schema, which this dump does not include. Restoring into a new Supabase project would need owners to sign up again, or a separate export of the accounts. To decide before launch.
- **A real Supabase project as the target.** Not rehearsed, because the organisation was at its limit of two free projects. The steps should carry over; one run is worth doing when a project slot is free.
- **Point-in-time recovery.** Anything written after the last manual dump is lost. Supabase's documentation lists daily backups from the Pro plan upward; the backup plan for launch is an open decision.
- **Size.** Timings are for today's small staging data.
- **Provider retries.** How long Stripe keeps retrying a notification while the database is down was not checked.

## 5. Findings

1. Two real-database tests were out of date with migrations 0016 and 0019, so they failed on any up-to-date database. Fixed on `main` (commit `635a08e`).
2. `test_an_event_without_an_identifier_is_still_applied` counted every row in `payment_events`, so it failed on a database that already held events. Fixed: it now compares the count before and after.
3. The real-database tests do not run in CI, which is how finding 1 went unnoticed. Still open.

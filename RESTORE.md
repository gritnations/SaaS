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

To check the repository's migrations alone, skip steps 2 to 6: run the migration runner (section 6) against an empty database, then run the test suite with `BOOKING_TEST_DATABASE_URL` pointing at it.

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

## 6. Apply migrations with the runner

`scripts/migrate.py` applies the files in `migrations/` in order, each one completely or not at all, and records each in the `schema_migrations` ledger. Take a backup first (section 2).

```bash
# MIGRATION_DATABASE_URL must be the database owner's connection string. Never print it or commit it.
MIGRATION_DATABASE_URL=... python -m scripts.migrate --status   # list applied and pending files; change nothing
MIGRATION_DATABASE_URL=... python -m scripts.migrate            # apply what is pending
```

- An empty database gets 0001 to 0015 in one transaction. 0015 creates the ledger and records all fifteen.
- Each later file runs in its own transaction. If a statement fails, or the file does not record itself in the ledger, the transaction is rolled back and the run stops with exit code 1.
- A database with tables but no ledger, or with a ledger that names an unknown file or skips one, is refused before anything runs.
- A file that opens or closes its own transaction is refused before anything runs. New files keep ending with their own ledger insert, as 0016 to 0019 do.
- Each transaction first takes a database lock, so two runs at the same time do not apply the same file. A run that finds a file already applied by the other prints "Already applied by another run" for it and does not count it.

**Rehearsed on 23 November 2026**, against a local PostgreSQL 17 server:

- An empty database: `--status` listed 19 pending files; the run applied all 19; the database then had 9 tables and 19 ledger rows. A second run printed "Nothing to apply."
- The restored copy of staging (from section 1): `--status` showed 19 of 19 applied; a run printed "Nothing to apply." and changed nothing.
- A deliberately failing file 0020 (a table, then a division by zero, then its ledger row): the run stopped with exit code 1; the table was not created and the ledger still had 19 rows.
- A database with one unrelated table and no ledger: refused with exit code 1; nothing was created.
- The runner's real-database tests (`tests/test_migrate_postgres.py`): 7 passed. The full suite on the rebuilt database: 907 passed, 1 skipped.
- Two runs at the same time (tested 23 November, `test_two_runs_at_the_same_time_apply_each_file_once`, run 5 times): both planned the empty database and waited on the lock; every file was applied once, by one of them, neither failed, and the applied counts added up to 19. Before this was fixed, both runs reported all 19 as applied.

Not rehearsed: running it against the hosted database, and a failure in the middle of 0001 to 0015 on a real server (covered only by a test on the local server).

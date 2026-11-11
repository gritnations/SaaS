-- 0015: a ledger of applied migrations, so the database itself records which of these files it has had.
--
-- Forward-only. Apply once, after 0014, as the database owner.
--
-- Until now the applied migrations were known only from memory and from the shape of the tables. This table
-- records one row per migration file. From this migration on, every migration file ends with an INSERT of its
-- own version into schema_migrations, and a test enforces that. Rows 0001 to 0014 are written here because
-- they were applied, in order, before this one: a database built from these files has all of them, and the
-- staging database is checked against them before this file is applied to it.
--
-- The API has no access to this table. Only the database owner reads and writes it.

CREATE TABLE schema_migrations (
    version    text PRIMARY KEY CHECK (version ~ '^[0-9]{4}_[a-z0-9_]+$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Same protection as the other tables: no policies, and the Data API roles cannot reach it.
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON schema_migrations FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON schema_migrations FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
        REVOKE ALL ON schema_migrations FROM app_api;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES
    ('0001_booking'),
    ('0002_example_business'),
    ('0003_business_members'),
    ('0004_manage_and_emails'),
    ('0005_business_signup'),
    ('0006_stripe_payments'),
    ('0007_paid_plans'),
    ('0008_trial_and_payment_options'),
    ('0009_plan_end_date'),
    ('0010_public_contact'),
    ('0011_time_off'),
    ('0012_booking_refunded'),
    ('0013_payment_provider'),
    ('0014_app_api_role'),
    ('0015_migration_ledger');

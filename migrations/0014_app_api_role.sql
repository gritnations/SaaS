-- 0014: a limited database role for the API, instead of the database owner.
--
-- Forward-only. Apply once, after 0013, as the database owner.
--
-- Until now the API connected as the owner, which can create roles and databases and change or drop any
-- table. app_api holds only the privileges the code uses (read from the SQL in booking/, api/ and payments/):
-- no schema changes, no role creation, no deleting bookings, and no reading or changing the email log beyond
-- adding to it. It bypasses row-level security on purpose, as the owner does today: isolation between
-- businesses stays in the application code. Row policies per business are a separate, later step.
--
-- The role has no password here. Set one by hand, outside the repository, before it is used:
--     ALTER ROLE app_api PASSWORD '...';
-- Until then it cannot sign in. Every later migration that adds a table must grant it explicitly.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
        CREATE ROLE app_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS;
    END IF;
END
$$;

-- The owner may switch to the role (for tests and checks) but does not inherit its privileges.
GRANT app_api TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_api;
GRANT USAGE ON SCHEMA public TO app_api;

GRANT SELECT, INSERT, UPDATE ON businesses, services, bookings TO app_api;
GRANT SELECT, INSERT ON business_members TO app_api;
GRANT INSERT ON email_log TO app_api;
GRANT SELECT, INSERT, DELETE ON time_off, weekly_hours TO app_api;

-- 0003: who may manage a business.
--
-- Forward-only. Apply once, after 0001, as the database owner.
--
-- user_id is the subject of a verified sign-in token: the identifier the identity provider gives the
-- account. It is not a foreign key to the provider's own tables, so this schema does not depend on them.
-- Linking an account to a business is an administrative step; there is no self-service sign-up yet.
--
-- Row level security is enabled with no policies and the Data API roles are revoked, as for the
-- booking tables. Only the platform API, connecting as the owner, reaches this table.

CREATE TABLE business_members (
    business_id uuid NOT NULL REFERENCES businesses (id),
    user_id     uuid NOT NULL,
    role        text NOT NULL CHECK (role IN ('owner')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, user_id)
);

CREATE INDEX business_members_user_idx ON business_members (user_id);

ALTER TABLE business_members ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON business_members FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON business_members FROM authenticated;
    END IF;
END
$$;

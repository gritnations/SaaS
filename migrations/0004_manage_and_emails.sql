-- 0004: customer manage links, cancellation and move rules, and the email log.
--
-- Forward-only. Apply once, after 0003, as the database owner.
--
-- A booking's manage code is never stored: only its SHA-256 hash is. Bookings made before this
-- migration have no hash and cannot be managed by link; their owner can still cancel them.

ALTER TABLE businesses
    ADD COLUMN cancel_cutoff_minutes integer NOT NULL DEFAULT 1440 CHECK (cancel_cutoff_minutes BETWEEN 0 AND 43200),
    ADD COLUMN move_cutoff_minutes   integer NOT NULL DEFAULT 1440 CHECK (move_cutoff_minutes BETWEEN 0 AND 43200),
    ADD COLUMN contact_email         text CHECK (contact_email IS NULL OR length(contact_email) BETWEEN 3 AND 254),
    ADD COLUMN meeting_link          text CHECK (meeting_link IS NULL OR (meeting_link ~ '^https://' AND length(meeting_link) <= 500));

ALTER TABLE bookings
    ADD COLUMN manage_token_hash bytea CHECK (manage_token_hash IS NULL OR length(manage_token_hash) = 32),
    ADD COLUMN cancelled_at      timestamptz,
    ADD COLUMN cancelled_by      text CHECK (cancelled_by IS NULL OR cancelled_by IN ('customer', 'owner')),
    ADD COLUMN move_count        integer NOT NULL DEFAULT 0 CHECK (move_count >= 0),
    ADD CONSTRAINT bookings_cancel_consistent CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL));

-- One row per email the platform tried to send. Holds no message body and no address: the recipient
-- is identified by role, and an error is a short provider status, never a message containing data.
CREATE TABLE email_log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id    uuid NOT NULL REFERENCES businesses (id),
    booking_id     uuid REFERENCES bookings (id),
    kind           text NOT NULL CHECK (kind IN ('confirmed', 'cancelled', 'moved')),
    recipient_role text NOT NULL CHECK (recipient_role IN ('customer', 'owner')),
    status         text NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')),
    provider_id    text CHECK (provider_id IS NULL OR length(provider_id) <= 100),
    error          text CHECK (error IS NULL OR length(error) <= 200),
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX email_log_booking_idx ON email_log (booking_id);

ALTER TABLE email_log ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON email_log FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON email_log FROM authenticated;
    END IF;
END
$$;

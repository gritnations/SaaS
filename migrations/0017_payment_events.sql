-- 0017: a record of each payment-provider event that changed a booking.
--
-- Forward-only. Apply once, after 0016, as the database owner.
--
-- A provider can send the same notification more than once. A booking's own state already stops a repeat from
-- being applied twice; this table also recognises a repeat by the provider's event identifier, and keeps a
-- durable record that the event was processed. A row is written in the same transaction as the booking change,
-- so an event that failed or changed nothing leaves no row and can be delivered again.
--
-- The table is owner-only apart from one privilege: the API role may insert rows and nothing else. It cannot read,
-- change or delete them, and the public roles of the hosted database hold nothing. Row security is on.
--
-- The table, its privileges and the ledger row are written in one DO block, so they succeed or fail together,
-- whichever tool applies the file.

DO $$
BEGIN
    CREATE TABLE payment_events (
        provider    text        NOT NULL CHECK (length(provider) BETWEEN 1 AND 50),
        event_id    text        NOT NULL CHECK (length(event_id) BETWEEN 1 AND 255),
        received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (provider, event_id)
    );

    ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON payment_events FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON payment_events FROM authenticated;
    END IF;

    REVOKE ALL ON payment_events FROM app_api;
    GRANT INSERT ON payment_events TO app_api;

    INSERT INTO schema_migrations (version) VALUES ('0017_payment_events');
END
$$;

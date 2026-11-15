-- 0018: a booking can remember the client's idempotency key, so a repeated request returns the same booking.
--
-- Forward-only. Apply once, after 0017, as the database owner.
--
-- A customer's page sends one random key with each booking attempt. If the answer is lost and the page sends the
-- same request again, the service finds the booking already made under that key and returns it, instead of
-- refusing the repeat because its time is now taken. The request's hash is stored with the key, so the same key
-- used for a different request is refused, not answered with someone else's booking.
--
-- Both columns are optional, and a booking made without a key is unchanged. A key is unique within a business.
-- The existing table-level privileges of the API role cover the new columns; nothing is granted here.
--
-- The columns, the check, the index and the ledger row are written in one DO block, so they succeed or fail
-- together, whichever tool applies the file.

DO $$
BEGIN
    ALTER TABLE bookings
        ADD COLUMN idempotency_key text
            CONSTRAINT bookings_idempotency_key_shape CHECK (idempotency_key IS NULL OR idempotency_key ~ '^[A-Za-z0-9_-]{8,128}$'),
        ADD COLUMN request_hash bytea
            CONSTRAINT bookings_request_hash_length CHECK (request_hash IS NULL OR octet_length(request_hash) = 32),
        ADD CONSTRAINT bookings_idempotency_complete CHECK ((idempotency_key IS NULL) = (request_hash IS NULL));

    CREATE UNIQUE INDEX bookings_idempotency_per_business ON bookings (business_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

    INSERT INTO schema_migrations (version) VALUES ('0018_booking_idempotency_key');
END
$$;

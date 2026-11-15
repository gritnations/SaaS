-- 0019: a booking that holds a payment identifier must also record which gateway took the payment.
--
-- Forward-only. Apply once, after 0018, as the database owner.
--
-- The one-payment-one-booking index (0016) compares the pair (payment_provider, payment_id). A row with a payment
-- identifier and no gateway is not compared, because empty values are never equal in a unique index. This check
-- closes that gap for every write path: the payment code already writes the two together, and no such row exists
-- on staging. A gateway with no payment identifier is still allowed (a paid notification can carry none), and so
-- is a booking with neither.
--
-- The constraint is checked against every existing row when it is added. If a row breaks it in another database,
-- this statement fails and nothing is changed. Adding it briefly locks the bookings table while it reads it.
--
-- The constraint and the ledger row are written in one DO block, so they succeed or fail together, whichever
-- tool applies the file.

DO $$
BEGIN
    ALTER TABLE bookings
        ADD CONSTRAINT bookings_payment_needs_provider CHECK (payment_id IS NULL OR payment_provider IS NOT NULL);

    INSERT INTO schema_migrations (version) VALUES ('0019_payment_needs_provider');
END
$$;

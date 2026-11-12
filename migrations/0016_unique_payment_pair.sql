-- 0016: one payment can belong to only one booking.
--
-- Forward-only. Apply once, after 0015, as the database owner.
--
-- A booking records the gateway that took its payment (payment_provider) and that gateway's payment identifier
-- (payment_id). Until now nothing stopped two bookings from holding the same pair, so a repeated or mismatched
-- notification could have tied one payment to two appointments. This unique index refuses the second one.
--
-- The rule covers only bookings that have a payment identifier; free and pay-at-venue bookings have none and are
-- not affected. It is checked against the staging data before this file is applied: no pair is repeated there.
-- If a repeated pair exists in another database, this statement fails and nothing is changed.

CREATE UNIQUE INDEX bookings_payment_pair_key ON bookings (payment_provider, payment_id) WHERE payment_id IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('0016_unique_payment_pair');

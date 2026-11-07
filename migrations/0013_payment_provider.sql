-- 0013: which payment gateway took a booking's payment, so a refund goes back through the same one.
--
-- Forward-only. Apply once, after 0012, as the database owner.
--
-- Only Stripe has ever taken a booking payment, so every booking already paid or refunded is stamped 'stripe'.

ALTER TABLE bookings
    ADD COLUMN payment_provider text CHECK (payment_provider IS NULL OR length(payment_provider) BETWEEN 1 AND 50);

UPDATE bookings SET payment_provider = 'stripe' WHERE payment_id IS NOT NULL;

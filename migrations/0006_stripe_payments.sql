-- 0006: upfront online payment for paid services, through each business's own payment account.
--
-- Forward-only. Apply once, after 0005, as the database owner.
--
-- A paid booking starts as a hold (status pending_payment) that blocks its time like a confirmed booking
-- until hold_expires_at. It becomes confirmed only when a verified provider notification says it was
-- paid. An unpaid hold becomes expired: lazily, when a later booking needs the time, or when the provider
-- reports the checkout expired. No scheduler is needed.

ALTER TABLE businesses
    ADD COLUMN payment_account_id text UNIQUE
        CHECK (payment_account_id IS NULL OR payment_account_id ~ '^[A-Za-z0-9_]{1,100}$'),
    ADD COLUMN payments_enabled boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT businesses_payments_need_account CHECK (NOT payments_enabled OR payment_account_id IS NOT NULL);

ALTER TABLE bookings DROP CONSTRAINT bookings_status_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_status_check
    CHECK (status IN ('pending_payment', 'confirmed', 'cancelled', 'expired'));

ALTER TABLE bookings
    ADD COLUMN payment_status text NOT NULL DEFAULT 'not_required'
        CHECK (payment_status IN ('not_required', 'awaiting', 'paid')),
    ADD COLUMN hold_expires_at timestamptz,
    ADD COLUMN checkout_id text UNIQUE CHECK (checkout_id IS NULL OR length(checkout_id) BETWEEN 1 AND 255),
    ADD COLUMN payment_id text CHECK (payment_id IS NULL OR length(payment_id) BETWEEN 1 AND 255),
    ADD CONSTRAINT bookings_hold_has_expiry CHECK (status <> 'pending_payment' OR hold_expires_at IS NOT NULL),
    ADD CONSTRAINT bookings_hold_awaits_payment CHECK (status <> 'pending_payment' OR payment_status = 'awaiting');

-- Holds block time exactly as confirmed bookings do.
ALTER TABLE bookings DROP CONSTRAINT bookings_no_overlap;
ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap EXCLUDE USING gist (
    business_id WITH =,
    tstzrange(starts_at, blocked_until, '[)') WITH &&
) WHERE (status IN ('confirmed', 'pending_payment'));

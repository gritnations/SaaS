-- 0008: every business starts free with a 14-day trial and upgrades later; each business chooses how its
-- customers pay.
--
-- Forward-only. Apply once, after 0007, as the database owner.
--
-- Replaces the pay-at-sign-up model of 0007: a business is never closed waiting for its plan payment, so
-- the awaiting_payment status goes. A trial is a date, not a status: until trial_ends_at the business has
-- the paid features. Online card payments need a paid plan or the trial; paying at the venue works on
-- every plan. Which of the two a business offers is its own choice.

UPDATE businesses SET plan = 'free', plan_status = 'active' WHERE plan_status = 'awaiting_payment';

ALTER TABLE businesses DROP CONSTRAINT businesses_plan_status_check;
ALTER TABLE businesses ADD CONSTRAINT businesses_plan_status_check CHECK (plan_status IN ('active', 'past_due'));

ALTER TABLE businesses
    ADD COLUMN trial_ends_at timestamptz,
    ADD COLUMN pay_online boolean NOT NULL DEFAULT false,
    ADD COLUMN pay_at_venue boolean NOT NULL DEFAULT true;

-- A business already taking card payments keeps offering them.
UPDATE businesses SET pay_online = true WHERE payments_enabled;

ALTER TABLE bookings DROP CONSTRAINT bookings_payment_status_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_payment_status_check
    CHECK (payment_status IN ('not_required', 'awaiting', 'paid', 'at_venue'));

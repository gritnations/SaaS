-- 0007: paid plans. A business may subscribe to a paid plan, billed monthly to the platform's own payment
-- account (not to the business's account).
--
-- Forward-only. Apply once, after 0006, as the database owner.
--
-- A business on a paid plan starts as awaiting_payment and becomes active only when a verified provider
-- notification says the subscription is paid. Its public booking page stays closed until then. A failed
-- renewal makes it past_due (still open, with a warning to the owner); an ended subscription returns the
-- business to the free plan.

ALTER TABLE businesses DROP CONSTRAINT businesses_plan_check;
ALTER TABLE businesses ADD CONSTRAINT businesses_plan_check CHECK (plan IN ('free', 'starter', 'pro'));

ALTER TABLE businesses
    ADD COLUMN plan_status text NOT NULL DEFAULT 'active'
        CHECK (plan_status IN ('active', 'awaiting_payment', 'past_due')),
    ADD COLUMN billing_customer_id text UNIQUE
        CHECK (billing_customer_id IS NULL OR billing_customer_id ~ '^[A-Za-z0-9_]{1,100}$'),
    ADD COLUMN billing_subscription_id text UNIQUE
        CHECK (billing_subscription_id IS NULL OR billing_subscription_id ~ '^[A-Za-z0-9_]{1,100}$'),
    ADD CONSTRAINT businesses_free_plan_is_active CHECK (plan <> 'free' OR plan_status = 'active');

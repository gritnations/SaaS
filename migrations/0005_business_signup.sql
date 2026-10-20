-- 0005: business self sign-up.
--
-- Forward-only. Apply once, after 0004, as the database owner.
--
-- A business now records what kind of business it is and which plan it is on. Only the free plan exists;
-- paid plans are added when they are decided. The owner's name is stored on the membership, not on the
-- business, so it is never part of anything shown to customers.
--
-- One account may manage at most one business for now, enforced by a unique index.

ALTER TABLE businesses
    ADD COLUMN business_type text CHECK (
        business_type IS NULL OR business_type IN (
            'barbershop', 'car_wash', 'clinic', 'consulting', 'education', 'events',
            'spa', 'photographer', 'salon', 'fitness', 'other'
        )
    ),
    ADD COLUMN business_type_other text CHECK (business_type_other IS NULL OR length(business_type_other) BETWEEN 1 AND 60),
    ADD COLUMN plan text NOT NULL DEFAULT 'free' CHECK (plan IN ('free')),
    ADD CONSTRAINT businesses_other_type_described CHECK (business_type IS DISTINCT FROM 'other' OR business_type_other IS NOT NULL);

ALTER TABLE business_members
    ADD COLUMN display_name text CHECK (display_name IS NULL OR length(display_name) BETWEEN 1 AND 100);

CREATE UNIQUE INDEX business_members_one_business_per_account ON business_members (user_id);

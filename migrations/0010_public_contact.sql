-- 0010: contact details customers see on the booking and manage pages.
--
-- Forward-only. Apply once, after 0009, as the database owner.
--
-- These are public. They are separate from contact_email, which is where the business receives booking
-- emails and is never shown to customers. All three are optional; a page shows only what is set.

ALTER TABLE businesses
    ADD COLUMN public_address text CHECK (public_address IS NULL OR length(public_address) BETWEEN 3 AND 200),
    ADD COLUMN public_phone   text CHECK (public_phone IS NULL OR length(public_phone) BETWEEN 5 AND 32),
    ADD COLUMN public_email   text CHECK (public_email IS NULL OR length(public_email) BETWEEN 3 AND 254);

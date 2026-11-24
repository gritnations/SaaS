-- 0021: the look of a business's booking pages, and its logo.
--
-- Forward-only. Apply once, after 0020, as the database owner.
--
-- An owner chooses colours, a font and a corner style from fixed sets, and may upload a logo. Nothing here is
-- free-form styling: every value is checked against its set, so no owner can put code into a customer's page.
--     * booking_looks: at most two rows per business, a draft and a published look. Customers see the
--       published one, and only while the business's plan or trial includes branding; the application checks
--       that. No row means the default look.
--     * business_logos: uploaded images, PNG, JPEG or WebP only, at most 60 KB. A look points to one. The
--       application checks the file's own bytes as well as its declared type.
--
-- Row security is on and the hosted database's public roles hold nothing, as for every other table. The API
-- role may read, add, change and remove looks, and read, add and remove logos; it never changes a logo in place.
--
-- Everything, including the ledger row, is in one DO block, so it succeeds or fails together whichever tool
-- applies the file.

DO $$
BEGIN
    CREATE TABLE business_logos (
        id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id  uuid        NOT NULL REFERENCES businesses (id),
        content_type text        NOT NULL CHECK (content_type IN ('image/png', 'image/jpeg', 'image/webp')),
        data         bytea       NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 61440),
        created_at   timestamptz NOT NULL DEFAULT now()
    );

    CREATE INDEX business_logos_business_idx ON business_logos (business_id);

    CREATE TABLE booking_looks (
        business_id uuid        NOT NULL REFERENCES businesses (id),
        state       text        NOT NULL CHECK (state IN ('draft', 'published')),
        accent      text        NOT NULL CHECK (accent ~ '^#[0-9a-f]{6}$'),
        background  text        NOT NULL CHECK (background ~ '^#[0-9a-f]{6}$'),
        text_colour text        NOT NULL CHECK (text_colour ~ '^#[0-9a-f]{6}$'),
        font        text        NOT NULL CHECK (font IN ('modern', 'classic', 'rounded', 'geometric')),
        corners     text        NOT NULL CHECK (corners IN ('sharp', 'soft', 'round')),
        logo_id     uuid        REFERENCES business_logos (id) ON DELETE SET NULL,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, state)
    );

    ALTER TABLE business_logos ENABLE ROW LEVEL SECURITY;
    ALTER TABLE booking_looks ENABLE ROW LEVEL SECURITY;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON business_logos, booking_looks FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON business_logos, booking_looks FROM authenticated;
    END IF;

    REVOKE ALL ON business_logos, booking_looks FROM app_api;
    GRANT SELECT, INSERT, DELETE ON business_logos TO app_api;
    GRANT SELECT, INSERT, UPDATE, DELETE ON booking_looks TO app_api;

    INSERT INTO schema_migrations (version) VALUES ('0021_booking_look');
END
$$;

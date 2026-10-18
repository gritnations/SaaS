-- 0001: booking domain, slice 1 (public booking of free services).
--
-- Forward-only. Apply once, in order, as the database owner. Every table carries business_id so that
-- data is scoped to one business from the start; how businesses are isolated beyond that is not decided.
--
-- Two invariants are enforced here, in the database, not only in application code:
--   * a service belongs to exactly one business, and a booking can only reference a service of its own
--     business (composite foreign key);
--   * two active bookings of the same business can never overlap, including the break that follows each
--     booking (exclusion constraint on [starts_at, blocked_until)).
--
-- Row level security is enabled with no policies, so the Supabase Data API roles (anon, authenticated)
-- can read or write nothing. Only the platform API, connecting as the owner, reaches these tables.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE businesses (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug           text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'),
    name           text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    time_zone      text NOT NULL DEFAULT 'Europe/London',
    currency       char(3) NOT NULL DEFAULT 'GBP' CHECK (currency ~ '^[A-Z]{3}$'),
    break_minutes  integer NOT NULL DEFAULT 0 CHECK (break_minutes BETWEEN 0 AND 240),
    notice_minutes integer NOT NULL DEFAULT 240 CHECK (notice_minutes BETWEEN 0 AND 43200),
    window_days    integer NOT NULL DEFAULT 30 CHECK (window_days BETWEEN 1 AND 365),
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE services (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id      uuid NOT NULL REFERENCES businesses (id),
    slug             text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$'),
    name             text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    duration_minutes integer NOT NULL CHECK (duration_minutes BETWEEN 5 AND 480),
    price_minor      integer NOT NULL CHECK (price_minor >= 0),
    active           boolean NOT NULL DEFAULT true,
    sort_order       integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (business_id, slug),
    UNIQUE (business_id, id)
);

-- One row per working block. Several blocks on one weekday give that day a gap, such as lunch.
-- weekday: 0 = Sunday ... 6 = Saturday. Times are wall-clock times in the business's time zone.
CREATE TABLE weekly_hours (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid NOT NULL REFERENCES businesses (id),
    weekday     smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time  time NOT NULL,
    end_time    time NOT NULL,
    CHECK (end_time > start_time)
);

CREATE INDEX weekly_hours_business_idx ON weekly_hours (business_id, weekday);

CREATE TABLE bookings (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id    uuid NOT NULL REFERENCES businesses (id),
    service_id     uuid NOT NULL,
    reference      text NOT NULL UNIQUE CHECK (reference ~ '^[A-Z0-9]{10}$'),
    status         text NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    starts_at      timestamptz NOT NULL,
    ends_at        timestamptz NOT NULL,
    -- ends_at plus the business's break at the time of booking. The exclusion constraint uses it, so a
    -- new booking can neither start nor end inside the break around an existing one.
    blocked_until  timestamptz NOT NULL,
    price_minor    integer NOT NULL CHECK (price_minor >= 0),
    customer_name  text NOT NULL CHECK (length(customer_name) BETWEEN 1 AND 100),
    customer_email text NOT NULL CHECK (length(customer_email) BETWEEN 3 AND 254),
    customer_phone text CHECK (customer_phone IS NULL OR length(customer_phone) BETWEEN 5 AND 32),
    customer_note  text CHECK (customer_note IS NULL OR length(customer_note) <= 1000),
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (business_id, service_id) REFERENCES services (business_id, id),
    CHECK (ends_at > starts_at),
    CHECK (blocked_until >= ends_at),
    CONSTRAINT bookings_no_overlap EXCLUDE USING gist (
        business_id WITH =,
        tstzrange(starts_at, blocked_until, '[)') WITH &&
    ) WHERE (status = 'confirmed')
);

CREATE INDEX bookings_business_start_idx ON bookings (business_id, starts_at);

ALTER TABLE businesses ENABLE ROW LEVEL SECURITY;
ALTER TABLE services ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_hours ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON businesses, services, weekly_hours, bookings FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON businesses, services, weekly_hours, bookings FROM authenticated;
    END IF;
END
$$;

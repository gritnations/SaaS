-- 0011: days off. A business can close single days, or a run of days, on top of its weekly opening hours.
--
-- Forward-only. Apply once, after 0010, as the database owner.
--
-- A day off is whole local days in the business's own time zone, both ends included. The note is private to
-- the owner. A day off never touches a booking that already exists: availability skips the days, nothing more.

CREATE TABLE time_off (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid NOT NULL REFERENCES businesses (id),
    first_day   date NOT NULL,
    last_day    date NOT NULL,
    note        text CHECK (note IS NULL OR length(note) <= 100),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (last_day >= first_day AND last_day - first_day <= 365)
);

CREATE INDEX time_off_business_idx ON time_off (business_id, first_day, last_day);

-- Same protection as the other tables: no policies, and the Data API roles cannot reach it.
ALTER TABLE time_off ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON time_off FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON time_off FROM authenticated;
    END IF;
END
$$;

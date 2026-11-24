-- 0020: reminder emails 24 hours before a booking.
--
-- Forward-only. Apply once, after 0019, as the database owner.
--
-- The reminder run reads bookings as they are now, so a cancelled or moved booking needs no extra step:
--     * moved_at: when the customer last moved the booking. A booking made or moved less than 24 hours before
--       its start gets no reminder, because its confirmation or move email has just gone out.
--     * reminder_starts_at: the start time a reminder was last claimed for. A run claims a booking by setting it
--       to the booking's start, in one statement that skips rows another run holds, so two runs never claim the
--       same booking. A booking moved to a new time no longer matches, and can be reminded again.
--     * reminder_attempts: claims made for the current time. A failed send clears the claim so a later run
--       retries; after three attempts the booking is left alone. A move resets it.
--
-- The email log accepts the new kind 'reminder'. The API role already updates bookings and adds to the log, so
-- no grant changes. Existing bookings start unreminded: one already inside its 24 hours when this is applied,
-- and made more than 24 hours before its start, is reminded on the first run.
--
-- Everything, including the ledger row, is in one DO block, so it succeeds or fails together whichever tool
-- applies the file.

DO $$
BEGIN
    ALTER TABLE bookings
        ADD COLUMN moved_at           timestamptz,
        ADD COLUMN reminder_starts_at timestamptz,
        ADD COLUMN reminder_attempts  smallint NOT NULL DEFAULT 0 CHECK (reminder_attempts BETWEEN 0 AND 10);

    ALTER TABLE email_log
        DROP CONSTRAINT email_log_kind_check,
        ADD CONSTRAINT email_log_kind_check CHECK (kind IN ('confirmed', 'cancelled', 'moved', 'reminder'));

    CREATE INDEX bookings_confirmed_start_idx ON bookings (starts_at) WHERE status = 'confirmed';

    INSERT INTO schema_migrations (version) VALUES ('0020_booking_reminders');
END
$$;

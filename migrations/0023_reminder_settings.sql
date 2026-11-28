-- 0023: reminder settings for each business, an earlier reminder, and when each reminder was sent.
--
-- Forward-only. Apply once, after 0022, as the database owner.
--
-- A business chooses whether its customers get the 24-hour reminder and an earlier one, how early the earlier one
-- is (48 hours, 72 hours or a week) and an optional short note. Whether its plan includes the earlier reminder is
-- checked by the application, when reminders are claimed.
--
--     * businesses: reminder_day_enabled, reminder_early_enabled, reminder_early_hours, reminder_note. Existing
--       businesses start with both reminders on and the earlier one at 72 hours.
--     * bookings: reminder_early_starts_at and reminder_early_attempts claim the earlier reminder, as the columns
--       from 0020 claim the 24-hour one. reminder_sent_at and reminder_early_sent_at record when the email
--       provider accepted each. Reminders sent before this migration have no time recorded.
--
-- The email log accepts the new kind 'reminder_early'. The API role already reads and updates both tables and adds
-- to the log, so no grant changes.
--
-- Everything, including the ledger row, is in one DO block, so it succeeds or fails together whichever tool
-- applies the file.

DO $$
BEGIN
    ALTER TABLE businesses
        ADD COLUMN reminder_day_enabled   boolean  NOT NULL DEFAULT true,
        ADD COLUMN reminder_early_enabled boolean  NOT NULL DEFAULT true,
        ADD COLUMN reminder_early_hours   smallint NOT NULL DEFAULT 72
            CONSTRAINT businesses_reminder_early_hours CHECK (reminder_early_hours IN (48, 72, 168)),
        ADD COLUMN reminder_note          text
            CONSTRAINT businesses_reminder_note CHECK (reminder_note IS NULL OR length(reminder_note) BETWEEN 1 AND 200);

    ALTER TABLE bookings
        ADD COLUMN reminder_sent_at         timestamptz,
        ADD COLUMN reminder_early_starts_at timestamptz,
        ADD COLUMN reminder_early_attempts  smallint NOT NULL DEFAULT 0
            CONSTRAINT bookings_reminder_early_attempts CHECK (reminder_early_attempts BETWEEN 0 AND 10),
        ADD COLUMN reminder_early_sent_at   timestamptz;

    ALTER TABLE email_log
        DROP CONSTRAINT email_log_kind_check,
        ADD CONSTRAINT email_log_kind_check
            CHECK (kind IN ('confirmed', 'cancelled', 'moved', 'reminder', 'reminder_early'));

    INSERT INTO schema_migrations (version) VALUES ('0023_reminder_settings');
END
$$;

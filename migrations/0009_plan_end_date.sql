-- 0009: when a cancelled plan ends.
--
-- Forward-only. Apply once, after 0008, as the database owner.
--
-- A plan cancelled from the provider's portal usually stays active until the end of the period already
-- paid for. plan_ends_at records that date so the owner can be told; it is cleared when the cancellation
-- is undone or the plan ends. The plan itself still changes only when the provider reports the end.

ALTER TABLE businesses ADD COLUMN plan_ends_at timestamptz;

-- 0022: allow logos up to 100 KB.
--
-- Forward-only. Apply once, after 0021, as the database owner.
--
-- 0021 limited a logo to 60 KB, to stay under the API's 64 KB request body limit. The API now accepts a larger
-- body on the logo upload route only, so this relaxes the database's own check to match. Every logo already
-- stored is at most 60 KB, so no existing row is affected. The table's privileges and row security are unchanged.
--
-- Everything, including the ledger row, is in one DO block, so it succeeds or fails together whichever tool
-- applies the file. The old check has an automatic name, so it is found by what it checks.

DO $$
DECLARE
    old_check text;
BEGIN
    SELECT conname INTO old_check
    FROM pg_constraint
    WHERE conrelid = 'business_logos'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%octet_length(data)%';

    IF old_check IS NULL THEN
        RAISE EXCEPTION 'business_logos has no size check to replace; 0021 was not applied as expected';
    END IF;

    EXECUTE format('ALTER TABLE business_logos DROP CONSTRAINT %I', old_check);
    ALTER TABLE business_logos
        ADD CONSTRAINT business_logos_data_size CHECK (octet_length(data) BETWEEN 1 AND 102400);

    INSERT INTO schema_migrations (version) VALUES ('0022_logo_size');
END
$$;

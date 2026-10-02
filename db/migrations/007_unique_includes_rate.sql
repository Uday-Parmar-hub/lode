-- 007 — put `rate` in the royalties uniqueness key.
--
-- The old key was (source_docid, project_name, holder, royalty_type). Two royalties from one
-- document that agree on those four but differ in RATE were read as the same royalty, and the
-- second was dropped by ON CONFLICT DO NOTHING without a word.
--
-- That contradicts the code's own dedupe rule. collapse_repeats() in scripts/add_one_to_lode.py
-- keys on twelve fields — rate and every structured term — and its comment states the reasoning
-- exactly: "two royalties that agree on the rate but differ in their TERMS ... would look like
-- duplicates and the second would be silently dropped. Those are different instruments." The
-- constraint was narrower than the rule it was meant to back up; it predates collapse_repeats.
--
-- It cost real data. A Metalore release (mw-d0147949-1288-425d-9553-305bb35d15be) named a 1.5% NSR
-- on Cherbourg-Foxear and a 1.0% NSR on Walters-Leduc-Legault. Three royalties extracted, one
-- stored. Clicking again cannot recover them: the idempotency probe finds the row that DID store
-- and reports "already" without re-extracting.
--
-- Paired with the per-royalty `property_name` added to the extractor in the same change. That fixes
-- the cause (all three carried one project name); this fixes the consequence (the index could not
-- tell apart royalties that genuinely differ only in rate, e.g. a sliding scale stated as separate
-- instruments).
--
-- SAFE ON EXISTING DATA. Adding a column to a uniqueness key only ever permits MORE rows, never
-- fewer, so this cannot fail on rows already in the table and nothing needs cleaning up first.
-- Deliberately NOT `NULLS NOT DISTINCT`, which would be the stronger constraint but would fail
-- outright against the known duplicate groups (the same blocker that holds 005 back).

BEGIN;

-- Drop by shape, not by name: the old constraint was declared inline in schema.sql, so its name is
-- whatever Postgres generated, and that is not worth guessing from a migration.
DO $$
DECLARE
    cname text;
BEGIN
    SELECT con.conname INTO cname
      FROM pg_constraint con
     WHERE con.conrelid = 'royalties'::regclass
       AND con.contype = 'u'
       AND (
           -- ::text is load-bearing: pg_attribute.attname is `name`, and there is no
           -- `name[] = text[]` operator, so the comparison errors out without it.
           SELECT array_agg(att.attname::text ORDER BY att.attname::text)
             FROM unnest(con.conkey) AS k(attnum)
             JOIN pg_attribute att
               ON att.attrelid = con.conrelid AND att.attnum = k.attnum
       ) = ARRAY['holder', 'project_name', 'royalty_type', 'source_docid']
     LIMIT 1;

    IF cname IS NULL THEN
        RAISE NOTICE '007: no (source_docid, project_name, holder, royalty_type) unique constraint found — already migrated?';
    ELSE
        EXECUTE format('ALTER TABLE royalties DROP CONSTRAINT %I', cname);
        RAISE NOTICE '007: dropped %', cname;
    END IF;
END $$;

-- Guarded so the whole file is safe to run twice. Production's schema once sat four migrations
-- behind the code with no record of what had been applied, and the way out of that is migrations
-- you can re-run when you are not sure, rather than ones that abort and leave you no wiser.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'royalties'::regclass
           AND conname = 'royalties_docid_project_holder_type_rate_key'
    ) THEN
        RAISE NOTICE '007: royalties_docid_project_holder_type_rate_key already present — nothing to do';
    ELSE
        ALTER TABLE royalties
            ADD CONSTRAINT royalties_docid_project_holder_type_rate_key
            UNIQUE (source_docid, project_name, holder, royalty_type, rate);
        RAISE NOTICE '007: added royalties_docid_project_holder_type_rate_key';
    END IF;
END $$;

COMMIT;

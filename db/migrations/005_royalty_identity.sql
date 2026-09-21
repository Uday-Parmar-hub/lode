-- 005_royalty_identity.sql — a royalty's identity must include its RATE, and NULLs must not defeat it.
--
-- NOT YET APPLIED to any database. It refuses to run until the duplicates below are resolved, and it
-- must be deployed together with the code change noted at the bottom. Read this before applying.
--
-- The problem. Row identity is currently:
--     UNIQUE (source_docid, project_name, holder, royalty_type)
-- which is wrong in two ways:
--
--   1. The RATE is missing, so two genuinely different royalties stated in one document that share
--      project + holder + type (a 2% NSR and a separate 1% NSR both held by the vendors) collide, and
--      the ON CONFLICT DO NOTHING in scripts/load_royalties.py and scripts/add_one_to_lode.py throws
--      the second one away. Silently: the caller was told the insert succeeded.
--
--   2. Postgres compares NULLs as DISTINCT in a unique index, so when `holder` is NULL the constraint
--      does not apply at all and the same royalty re-inserts on every load. This is not theoretical —
--      in lode_test it produced 35 duplicate groups / 126 rows (Salares Norte's 2% NSR appears 14
--      times from ONE document), and in lode, 17 groups / 54 rows. Every one of those groups has a
--      NULL holder. It also inflates the corroboration signal: 53 instruments in lode have a
--      "reports for this royalty" count that is really one document read several times, not
--      independent sources agreeing.
--
-- Why this file does not clean the data. Choosing which of 14 near-identical rows to keep (and whether
-- any differ in a field this key ignores) is a review decision, not a migration. Resolve them first —
-- deterministically, keeping the lowest id per group, or via the reviewable-ledger pattern the rest of
-- this repo uses — then apply this. The guard below aborts the whole migration if any remain, so it
-- cannot leave the table half-migrated.
--
-- Also required, in the same deploy: both ON CONFLICT targets must name the new key, or every insert
-- fails with "no unique or exclusion constraint matching the ON CONFLICT specification":
--     scripts/load_royalties.py   ON CONFLICT (source_docid, project_name, holder, royalty_type)
--     scripts/add_one_to_lode.py  ON CONFLICT (source_docid, project_name, holder, royalty_type)
--   ->                            ON CONFLICT (source_docid, project_name, holder, royalty_type, rate)
-- Requires Postgres 15+ for NULLS NOT DISTINCT (local and Azure Flexible Server are 16; verify prod).

DO $$
DECLARE dups int;
BEGIN
  SELECT count(*) INTO dups FROM (
    SELECT 1 FROM royalties WHERE source_docid IS NOT NULL
     GROUP BY source_docid, project_name,
              coalesce(holder, '~null~'), coalesce(royalty_type, '~null~'), coalesce(rate, '~null~')
    HAVING count(*) > 1
  ) d;
  IF dups > 0 THEN
    RAISE EXCEPTION
      'refusing to apply 005: % duplicate royalty groups still present. Resolve them first (see the header of this file).', dups;
  END IF;
END $$;

ALTER TABLE royalties
  DROP CONSTRAINT IF EXISTS royalties_source_docid_project_name_holder_royalty_type_key;

-- NULLS NOT DISTINCT: a NULL holder or NULL rate now compares equal, so the constraint actually
-- applies to the rows it was silently skipping.
CREATE UNIQUE INDEX IF NOT EXISTS royalties_identity_key
  ON royalties (source_docid, project_name, holder, royalty_type, rate) NULLS NOT DISTINCT;

-- 003: memory-chain foundation (bucket C). Additive + non-destructive + idempotent.
-- Adds the versioned-instrument-chain scaffolding WITHOUT changing any behaviour yet:
--   instrument_id      - stable identity of a real royalty; a chain of version rows shares it.
--                        Backfilled one-per-existing-dup_key-group (a random, STABLE id — not derived
--                        from dup_key, so it survives future re-dedup; go-forward matching reuses it).
--   origin             - provenance of the row: claude | claude_human_edited | human | marketwatch.
--   needs_revalidation - set true when a new source/edit lands on a previously-validated instrument.
-- Nothing is deleted; existing columns/behaviour untouched; the live grid does not read these yet.

ALTER TABLE royalties ADD COLUMN IF NOT EXISTS instrument_id      TEXT;
ALTER TABLE royalties ADD COLUMN IF NOT EXISTS origin             TEXT;
ALTER TABLE royalties ADD COLUMN IF NOT EXISTS needs_revalidation BOOLEAN NOT NULL DEFAULT FALSE;

-- origin backfill from existing provenance
UPDATE royalties
   SET origin = CASE WHEN ingested_from = 'marketwatch' THEN 'marketwatch' ELSE 'claude' END
 WHERE origin IS NULL;

-- instrument_id backfill: one stable random id per current dup_key group ...
WITH grp AS (SELECT DISTINCT dup_key FROM royalties WHERE dup_key IS NOT NULL),
     ids AS (SELECT dup_key,
                    'inst_' || substr(md5(dup_key || clock_timestamp()::text || random()::text), 1, 20) AS iid
               FROM grp)
UPDATE royalties r
   SET instrument_id = ids.iid
  FROM ids
 WHERE r.dup_key = ids.dup_key AND r.instrument_id IS NULL;

-- ... and a per-row id for any row without a dup_key (should be none, but safe)
UPDATE royalties
   SET instrument_id = 'inst_' || substr(md5(id::text || clock_timestamp()::text || random()::text), 1, 20)
 WHERE instrument_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_roy_instrument ON royalties (instrument_id);
CREATE INDEX IF NOT EXISTS idx_roy_origin     ON royalties (origin);

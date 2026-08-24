-- 001: structured jurisdiction fields (Matt feedback, bucket B)
-- Additive only. Derived from the existing free-text `jurisdiction` by scripts/enrich_jurisdiction.py.
-- NOTE: the existing `tier` column is the analyst PRIORITY tier (human review layer). The jurisdiction
--       tier is a SEPARATE column (jurisdiction_tier) below — do not conflate the two.
-- Idempotent: safe to re-run (local + Azure).

ALTER TABLE royalties ADD COLUMN IF NOT EXISTS country           TEXT;
ALTER TABLE royalties ADD COLUMN IF NOT EXISTS state_province    TEXT;
ALTER TABLE royalties ADD COLUMN IF NOT EXISTS continent         TEXT;
ALTER TABLE royalties ADD COLUMN IF NOT EXISTS jurisdiction_tier SMALLINT;  -- 1=US/CA/AU; 2/3 pending Matt's list

CREATE INDEX IF NOT EXISTS idx_roy_country   ON royalties (country);
CREATE INDEX IF NOT EXISTS idx_roy_continent ON royalties (continent);

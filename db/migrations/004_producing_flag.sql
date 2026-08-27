-- 004_producing_flag.sql — binary "producing" flag (Matt's ask).
--
-- Keep the free-text `stage` (point-in-time, stamped with source_date) AND add a binary is_producing:
-- whether the property was IN PRODUCTION at the time of the source report. Claude extracts it directly
-- going forward; existing rows are backfilled from `stage` (scripts/backfill_producing.py).
-- Tri-state: true = producing, false = pre-production, NULL = unknown. Additive, non-destructive.

ALTER TABLE royalties ADD COLUMN IF NOT EXISTS is_producing boolean;
CREATE INDEX IF NOT EXISTS idx_roy_is_producing ON royalties (is_producing);

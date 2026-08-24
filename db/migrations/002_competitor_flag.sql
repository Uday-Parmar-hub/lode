-- 002: competitor-held flag (Matt feedback, bucket B)
-- Non-destructive (approach b): a DERIVED column recording which competitor (from OR_Competitor_List)
-- currently holds the instrument, if any. Does NOT modify royalty_available or the human score/review
-- layer. NULL = not held by a listed competitor (includes OR's own holdings and unlisted holders).
-- Populated by scripts/flag_competitors.py from a reviewable ledger.
-- OR Royalties (formerly Osisko Gold Royalties) is intentionally NOT a competitor.
-- Idempotent: safe to re-run (local + Azure).

ALTER TABLE royalties ADD COLUMN IF NOT EXISTS competitor_holder TEXT;
CREATE INDEX IF NOT EXISTS idx_roy_competitor ON royalties (competitor_holder);

-- 006_access_log.sql — in-house usage analytics for the LODE dashboard.
--
-- One row per authenticated page view (who, which page, when). Powers the unlisted /usage view.
-- Dashboard-owned: written by the Next.js app, not by any Python script.
--
-- The app also creates this lazily (create table if not exists on first write) because the prod
-- database is only reachable through Azure Cloud Shell — port 5432 is blocked from the office — so
-- a migration cannot be run against Azure from a dev machine. This file is the canonical shape.
--
-- Mirrors MarketWatch's db/migrations/014_access_log.sql, minus its `role` column: LODE has no role
-- concept, since Easy Auth tells us who the user is and nothing more.

CREATE TABLE IF NOT EXISTS access_log (
    id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email TEXT        NOT NULL,
    name  TEXT,
    path  TEXT        NOT NULL,
    at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_access_log_email_at ON access_log (email, at DESC);
CREATE INDEX IF NOT EXISTS idx_access_log_at ON access_log (at DESC);

-- LODE — Royalty Origination Intelligence · canonical schema (Postgres 16)
--
-- One row per THIRD-PARTY royalty found on an asset, extracted from a technical report and staged for
-- human review. Designed to hold Matt's full master-DB field set (33 cols) from day one:
--   • asset facts + royalty details  -> auto-filled from the report
--   • the 7 royalty-feature flags     -> auto-filled (disclosed in the report text)
--   • provenance (docid/quote/url)    -> the trust layer (every row cites its source sentence)
--   • the human layer (tier/rank/keep/score/comments) -> filled during review, never auto-committed
--
-- A canonical de-dup layer (one asset ↔ many reports) comes later; v1 keeps each extracted royalty as a
-- row and marks the best/newest per (asset, holder, type) as is_primary so the grid can default to it.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy search on names/holders

-- Review lifecycle: extracted rows land 'pending'; an analyst validates or rejects before they "count".
CREATE TYPE review_status AS ENUM ('pending', 'validated', 'rejected', 'needs_info');

-- How available the royalty is to acquire (Matt's "Royalty Available").
CREATE TYPE availability AS ENUM ('available', 'partial', 'held', 'unknown');

CREATE TABLE royalties (
    id                      BIGSERIAL PRIMARY KEY,

    -- ── Asset Details ─────────────────────────────────────────────────────
    sp_id                   TEXT,                 -- S&P Capital IQ id (canonical asset/company key)
    project_name            TEXT NOT NULL,
    operator                TEXT,
    commodity               TEXT[] NOT NULL DEFAULT '{}',   -- {Au,Cu,Mo} — array so we can filter by metal
    jurisdiction            TEXT,
    stage                   TEXT,                 -- exploration / PEA / PFS / FS / development / producing
    est_startup             TEXT,                 -- "Stage / Est. Start-Up" — the est. start year, if given

    -- structured jurisdiction (derived from `jurisdiction` free-text via scripts/enrich_jurisdiction.py)
    country                 TEXT,                 -- canonical country (e.g. "Canada", "United States")
    state_province          TEXT,                 -- primary state/province/region (NULL if country-level)
    continent               TEXT,                 -- derived from country
    jurisdiction_tier       SMALLINT,             -- 1=US/CA/AU; 2/3 from Matt's list (pending)

    -- ── Royalty Details ───────────────────────────────────────────────────
    royalty_type            TEXT,                 -- NSR / GSR / NPI / GVR / metal stream / ...
    rate                    TEXT,                 -- as stated: "2.00%", "0.7-1.3%", "US$5/t"
    rate_pct                NUMERIC,              -- parsed leading % for sort/filter (NULL if non-%)
    holder                  TEXT,                 -- Counterparties — the party entitled (the seller)
    holder_note             TEXT,                 -- e.g. "via Billiton Ecuador B.V. (now BHP)"
    royalty_available       availability NOT NULL DEFAULT 'unknown',
    extract_confidence      SMALLINT,             -- model confidence 1-5 (Royalty Details "Confidence")
    royalty_created         TEXT,                 -- Matt "Created" — granted date/context (semantics TBD w/ Matt)
    info_available          TEXT,                 -- what technical info exists (e.g. "2021 FS", "MRE 2018")

    -- ── Royalty Features (the 7 structured flags — disclosed in the report) ─
    partial_coverage        BOOLEAN,              -- burdens only part of the property
    advance_payments        TEXT,                 -- advance minimum royalty terms (NULL if none)
    production_threshold     TEXT,                -- payable only above a production threshold
    production_cap          TEXT,                 -- capped after N units / $
    buyback                 TEXT,                 -- buy-down / buy-back terms ("0.5% for US$2M before yr3")
    step_down               TEXT,                 -- sliding-scale / step-down structure
    rofr                    BOOLEAN,              -- right of first refusal / offer attached
    features_note           TEXT,                 -- anything else the analyst should check

    -- ── Provenance / trust ────────────────────────────────────────────────
    regime                  TEXT,                 -- NI 43-101 / S-K 1300 / JORC
    source_docid            TEXT,                 -- LSEG DocId or EDGAR accession:filename
    source_label            TEXT,                 -- "NI 43-101 · 2018"
    source_url              TEXT,                 -- archive URL (EDGAR) where available
    source_date             DATE,                 -- filing date of the source report
    source_quote            TEXT,                 -- the VERBATIM sentence stating this royalty
    quote_verified          BOOLEAN DEFAULT FALSE,-- quote found verbatim in the source text

    -- ── Human layer — Osisko Review (never auto-filled) ───────────────────
    tier                    SMALLINT,             -- analyst priority tier
    rank                    INTEGER,              -- analyst manual rank
    keep                    BOOLEAN,              -- keep in the DB vs discard
    status                  review_status NOT NULL DEFAULT 'pending',
    reviewed_by             TEXT,
    reviewed_at             TIMESTAMPTZ,

    -- ── Human layer — Osisko Score ────────────────────────────────────────
    score_project_quality    SMALLINT,
    score_instrument_quality  SMALLINT,
    score_confidence        SMALLINT,
    score_actionable        SMALLINT,

    -- ── Human layer — Comments ────────────────────────────────────────────
    comments                TEXT,
    link                    TEXT,

    -- ── Bookkeeping ───────────────────────────────────────────────────────
    is_primary              BOOLEAN NOT NULL DEFAULT TRUE,  -- best/newest row per asset-royalty (grid default)
    dup_key                 TEXT,                           -- canonical dedup key (normed asset|type|rate|holder); set by scripts/dedupe.py
    ingested_from           TEXT NOT NULL DEFAULT 'pilot',  -- pilot | universe | marketwatch
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),   -- "Date Entered"
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),    -- "Date Modified"

    -- a source can legitimately disclose several distinct royalties on one asset, so the dedupe key
    -- includes the holder + type, not just the doc.
    UNIQUE (source_docid, project_name, holder, royalty_type)
);

CREATE INDEX idx_roy_status       ON royalties (status);
CREATE INDEX idx_roy_available    ON royalties (royalty_available);
CREATE INDEX idx_roy_rate         ON royalties (rate_pct);
CREATE INDEX idx_roy_stage        ON royalties (stage);
CREATE INDEX idx_roy_commodity    ON royalties USING GIN (commodity);
CREATE INDEX idx_roy_primary      ON royalties (is_primary) WHERE is_primary;
CREATE INDEX idx_roy_dupkey       ON royalties (dup_key);
CREATE INDEX idx_roy_project_trgm ON royalties USING GIN (project_name gin_trgm_ops);
CREATE INDEX idx_roy_holder_trgm  ON royalties USING GIN (holder gin_trgm_ops);

-- keep updated_at honest
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_roy_touch BEFORE UPDATE ON royalties
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

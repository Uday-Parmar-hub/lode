"use server";

import Anthropic from "@anthropic-ai/sdk";
import { query, queryReadOnly, withTransaction } from "@/lib/db";

export interface ReviewPatch {
  status?: string; // review_status enum
  tier?: number | null;
  keep?: boolean | null;
  score_project_quality?: number | null;
  score_instrument_quality?: number | null;
  score_confidence?: number | null;
  score_actionable?: number | null;
  comments?: string | null;
  rank?: number | null;
  link?: string | null;
  availability?: string | null; // royalty_available enum: available | partial | held | unknown
}

/** Persist an analyst's review of one royalty. Nothing is "committed" until this runs (Matt's rule). */
export async function saveReview(id: string, p: ReviewPatch): Promise<{ ok: boolean }> {
  await query(
    `update royalties set
       status = coalesce($2::review_status, status),
       tier = $3, keep = $4,
       score_project_quality = $5, score_instrument_quality = $6,
       score_confidence = $7, score_actionable = $8,
       comments = $9, rank = $10, link = $11,
       royalty_available = coalesce($12::availability, royalty_available),
       reviewed_by = 'local', reviewed_at = now()
     where id = $1::bigint`,
    [id, p.status ?? null, p.tier ?? null, p.keep ?? null,
     p.score_project_quality ?? null, p.score_instrument_quality ?? null,
     p.score_confidence ?? null, p.score_actionable ?? null, p.comments ?? null,
     p.rank ?? null, p.link ?? null, p.availability ?? null],
  );
  return { ok: true };
}

/** The instrument "fact" fields an analyst can correct — an edit to any of these creates a NEW
 *  version in the instrument's chain (memory-chain), it never overwrites. Review-layer fields
 *  (tier/keep/scores/comments/validate) update in place via saveReview above. */
export interface FactEdit {
  royalty_type?: string | null;
  rate?: string | null;
  holder?: string | null;
  holder_note?: string | null;
  partial_coverage?: boolean | null;
  advance_payments?: string | null;
  production_threshold?: string | null;
  production_cap?: string | null;
  buyback?: string | null;
  step_down?: string | null;
  rofr?: boolean | null;
  features_note?: string | null;
}

/** Parse the leading percent of a rate string ("2.00%" -> 2, "US$5/oz" -> null) for sort/filter. */
function parseRatePct(rate: string | null | undefined): number | null {
  if (!rate) return null;
  const m = rate.match(/(\d+(?:\.\d+)?)\s*%/);
  return m ? Number(m[1]) : null;
}

/** Append-on-edit (memory-chain). A human correction to instrument facts inserts a NEW version row
 *  (origin=claude_human_edited, dated, needs_revalidation, status=pending) copied from the current row
 *  with the edits applied, then makes it the sole primary of its instrument chain — the prior row is
 *  retained as history (is_primary=false). Nothing is overwritten. Returns the new row id. */
export async function saveFactEdit(id: string, e: FactEdit): Promise<{ ok: boolean; newId?: string }> {
  const ratePct = parseRatePct(e.rate);
  return withTransaction(async (c) => {
    const ins = await c.query(
      `INSERT INTO royalties (
         sp_id, project_name, operator, commodity, jurisdiction, stage, est_startup,
         royalty_available, extract_confidence, royalty_created, info_available, regime,
         source_label, source_url, source_date, source_quote, quote_verified,
         tier, rank, keep, score_project_quality, score_instrument_quality, score_confidence, score_actionable,
         comments, link, ingested_from, dup_key, country, state_province, continent, jurisdiction_tier,
         competitor_holder, instrument_id,
         royalty_type, rate, rate_pct, holder, holder_note,
         partial_coverage, advance_payments, production_threshold, production_cap, buyback, step_down, rofr, features_note,
         source_docid, origin, status, is_primary, needs_revalidation, reviewed_by, reviewed_at, created_at, updated_at
       )
       SELECT
         sp_id, project_name, operator, commodity, jurisdiction, stage, est_startup,
         royalty_available, extract_confidence, royalty_created, info_available, regime,
         source_label, source_url, source_date, source_quote, quote_verified,
         tier, rank, keep, score_project_quality, score_instrument_quality, score_confidence, score_actionable,
         comments, link, ingested_from, dup_key, country, state_province, continent, jurisdiction_tier,
         competitor_holder, instrument_id,
         $2, $3, $4::numeric, $5, $6,
         $7::boolean, $8, $9, $10, $11, $12, $13::boolean, $14,
         coalesce(source_docid,'manual') || '#edit-' || extract(epoch from now())::bigint,
         'claude_human_edited', 'pending', true, true, null, null, now(), now()
       FROM royalties WHERE id = $1::bigint
       RETURNING id, instrument_id`,
      [id, e.royalty_type ?? null, e.rate ?? null, ratePct, e.holder ?? null, e.holder_note ?? null,
       e.partial_coverage ?? null, e.advance_payments ?? null, e.production_threshold ?? null,
       e.production_cap ?? null, e.buyback ?? null, e.step_down ?? null, e.rofr ?? null, e.features_note ?? null],
    );
    if (!ins.rows.length) return { ok: false };
    const { id: newId, instrument_id: iid } = ins.rows[0] as { id: string; instrument_id: string };
    // make the new version the sole primary of its chain; the prior version(s) become history
    await c.query(
      `UPDATE royalties SET is_primary = (id = $1::bigint) WHERE instrument_id = $2`,
      [newId, iid],
    );
    return { ok: true, newId: String(newId) };
  });
}

/** One version in an instrument's chain (for the history panel). */
export interface Version {
  id: string;
  is_primary: boolean;
  origin: string | null;
  status: string;
  needs_revalidation: boolean;
  source_label: string | null;
  source_date: string | null;
  holder: string | null;
  rate: string | null;
  royalty_type: string | null;
  quote_verified: boolean;
  created_at: string | null;
}

/** All versions of one instrument, newest/current first — powers the drawer's version-history panel.
 *  Collapses to ONE entry per source event: rows sharing a source_docid (intra-report extraction
 *  duplicates from a heavily-merged instrument) fold into a single representative (the primary /
 *  holder-bearing one); distinct reports and each human edit (unique '#edit-' docid) stay separate. */
export async function getInstrumentHistory(instrumentId: string): Promise<Version[]> {
  if (!instrumentId) return [];
  return query<Version>(
    `select id::text as id, is_primary, origin, status::text as status, needs_revalidation,
            source_label, source_date::text as source_date, holder, rate, royalty_type,
            quote_verified, created_at::text as created_at
       from (
         select distinct on (source_docid) *
           from royalties
          where instrument_id = $1
          order by source_docid, is_primary desc, (holder is not null) desc, created_at desc
       ) v
      order by is_primary desc, source_date desc nulls last, created_at desc`,
    [instrumentId],
  );
}

// ── AI search: natural language → SQL ─────────────────────────────────────────
// The analyst asks in plain English ("producing gold in Nevada under 2%", "top 10 holders by number
// of royalties", "average NSR by jurisdiction"). Claude writes ONE read-only SELECT over the royalties
// table. Safety is layered, not a single check: it runs as the SELECT-only `lode_ro` role inside a
// read-only transaction with a statement timeout (see lib/db.ts), AND the SQL is validated here to be a
// single SELECT with no data-modifying keywords. The model's SQL is shown to the user for transparency.

const SCHEMA_DOC = `Table: royalties  (one row per third-party royalty found on a mining asset)

Columns:
  id                bigint        -- row id
  is_primary        boolean       -- TRUE = the canonical, de-duplicated row. ALWAYS filter "where is_primary"
                                     unless the user explicitly asks about all source reports / duplicates.
  dup_key           text          -- dedup group id; count(*) grouped by dup_key = number of source reports for a royalty
  project_name      text          -- asset / project name (free text; use ILIKE)
  operator          text          -- company operating the asset (free text; use ILIKE)
  commodity         text[]        -- metal SYMBOLS: Au(gold) Ag(silver) Cu(copper) Ni(nickel) Zn(zinc) Mo(moly) PGE.
                                     Filter with: commodity && array['Au']  or  'Au' = any(commodity)
  jurisdiction      text          -- full free-text location, e.g. "Sonora, Mexico" (use ILIKE for granular/county-level).
  country           text          -- canonical country, e.g. 'Canada','United States','Chile'. Prefer this for country questions.
  state_province    text          -- primary state/province/region (NULL if country-level or multi-state)
  continent         text          -- 'North America'|'South America'|'Africa'|'Asia'|'Oceania'|'Europe'. Use for regional questions.
  jurisdiction_tier smallint      -- jurisdiction risk tier: 1 = US/Canada/Australia (2 & 3 not yet assigned; currently NULL)
  stage             text          -- exploration | PEA | PFS | FS | development | producing (free text; use ILIKE)
  est_startup       text
  royalty_type      text          -- NSR | NPI | GSR/GROSS | metal stream | ... (free text; use ILIKE)
  rate              text          -- rate as stated, e.g. "2.00%", "0.7-1.3%", "US$5/t"
  rate_pct          numeric       -- parsed leading percent (2 means 2%); NULL for non-% rates. Use for < > ranges.
  holder            text          -- the counterparty entitled to the royalty (free text; use ILIKE)
  competitor_holder text          -- if the holder is one of OR's competitors, the competitor's name; else NULL.
                                     "competitor-held" / "held by a competitor" -> competitor_holder is not null.
  holder_note       text
  royalty_available availability  -- enum: 'available' | 'partial' | 'held' | 'unknown'
  extract_confidence smallint     -- 1..5
  info_available    text
  partial_coverage  boolean
  advance_payments  text          -- NULL if none (IS NOT NULL = has advance payments); same idea for the next four:
  production_threshold text
  production_cap    text
  buyback           text
  step_down         text
  rofr              boolean
  features_note     text
  regime            text          -- 'NI 43-101' | 'S-K 1300' | 'JORC'
  source_label      text
  source_url        text
  source_date       date
  source_quote      text          -- the verbatim sentence from the report (use ILIKE for concept/full-text search)
  quote_verified    boolean
  status            review_status  -- enum: 'pending' | 'validated' | 'rejected' | 'needs_info'
  tier smallint, rank int, keep boolean
  score_project_quality smallint, score_instrument_quality smallint, score_confidence smallint, score_actionable smallint
  comments text, link text
  created_at timestamptz, updated_at timestamptz`;

const SYSTEM = `You translate a mining-royalty analyst's plain-English request into ONE PostgreSQL query over the
single table below. Output only the query and a one-sentence explanation.

${SCHEMA_DOC}

Rules:
- Exactly ONE read-only SELECT (or WITH ... SELECT). Never write, modify, or reference any other table. No semicolons.
- Default to the canonical de-duplicated view: include "is_primary" in the WHERE clause unless the user explicitly
  asks about all source reports, duplicates, or corroboration counts.
- Choose a mode:
  * "rows"  — the user wants to SEE / list / filter / find royalties. The SQL MUST select ONLY the id:
              select id from royalties where <...> [order by <...>] [limit <n>]
              (order by / limit are honored in the grid; cap large lists with a limit.)
  * "table" — the user asks a question answered by aggregation: counts, sums, averages, min/max, "how many",
              "per / by <x>", "top N <groups>", distributions. Return the answer columns directly, e.g.
              select holder, count(*) as royalties from royalties where is_primary group by holder order by royalties desc limit 20
- commodity is an array of metal symbols — convert metal names to symbols (gold->Au, copper->Cu, ...).
- rate_pct is a number (2 = 2%). "under 2%" -> rate_pct < 2. Use it for ranges; use rate only to display text.
- jurisdiction, operator, holder, project_name, stage, royalty_type are free text -> use ILIKE '%...%'.
- Use OR / NOT / ranges / ORDER BY / GROUP BY / aggregates freely. Always add a LIMIT for non-aggregate lists.
- "corroborated by the most reports" etc. -> group by dup_key or use a subquery counting rows per dup_key.
- explanation: one plain sentence describing what the query returns.
- STAY IN SCOPE. If the request is not answerable from this royalties table — general knowledge, chit-chat,
  jokes/stories/creative writing, math, coding, or anything unrelated to these mining royalties — set
  mode="reject", leave sql empty, and make explanation ONE friendly line saying what this search is for,
  e.g. "I only search the royalty database — try assets, holders, rates, jurisdictions, or an aggregate like
  'top holders by number of royalties'." Never invent a query for an unrelated request.`;

const SQL_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["mode", "sql", "explanation"],
  properties: {
    mode: { type: "string", enum: ["rows", "table", "reject"], description: "rows = list royalties in the grid; table = an aggregate answer; reject = the request isn't about the royalty database" },
    sql: { type: "string", description: "one read-only PostgreSQL SELECT over the royalties table; empty when mode=reject" },
    explanation: { type: "string", description: "one sentence: what the query returns, or (reject) what this search is for" },
  },
};

// SELECT-only gate (belt to the read-only role's braces): single statement, starts with select/with,
// no data-modifying or session-changing keywords, no comment markers that could hide a payload.
const FORBIDDEN =
  /\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|copy|vacuum|analyze|reindex|cluster|comment|call|do|merge|refresh|listen|notify|lock|set|reset|begin|commit|rollback|savepoint|prepare|execute|discard|import|dblink|pg_sleep|pg_read_file|pg_ls_dir|lo_import|lo_export|pg_terminate|pg_cancel)\b/i;

function validateSelect(sql: string): { ok: true; sql: string } | { ok: false; error: string } {
  const raw = sql.trim().replace(/;+\s*$/, "").trim();
  if (!raw) return { ok: false, error: "empty query" };
  if (raw.includes(";")) return { ok: false, error: "only a single statement is allowed" };
  if (raw.includes("--") || raw.includes("/*")) return { ok: false, error: "comments are not allowed" };
  if (!/^(with|select)\b/i.test(raw)) return { ok: false, error: "only SELECT queries are allowed" };
  if (FORBIDDEN.test(raw)) return { ok: false, error: "query contains a disallowed keyword" };
  return { ok: true, sql: raw };
}

function cell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}

export interface AiQueryResult {
  ok: boolean;
  mode: "rows" | "table" | "reject";
  explanation: string;
  sql: string;
  ids?: string[];          // rows mode: matching ids, in the query's order
  fields?: string[];       // table mode: column names
  rows?: string[][];       // table mode: display cells, aligned to fields
  error?: string;
}

/** Natural language -> one read-only SELECT (via Claude) -> matching rows for the grid, or an aggregate table. */
export async function aiQuery(nl: string): Promise<AiQueryResult> {
  const q = (nl ?? "").trim().slice(0, 500);
  const empty: AiQueryResult = { ok: true, mode: "rows", explanation: "", sql: "", ids: [] };
  if (!q) return empty;

  let spec: { mode?: string; sql?: string; explanation?: string };
  try {
    const client = new Anthropic();
    const res = (await client.messages.create({
      model: "claude-opus-5",
      max_tokens: 1200,
      output_config: { effort: "low", format: { type: "json_schema", schema: SQL_SCHEMA } },
      system: SYSTEM,
      messages: [{ role: "user", content: q }],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any)) as { content: { type: string; text?: string }[] };
    const text = res.content.find((b) => b.type === "text")?.text ?? "{}";
    spec = JSON.parse(text);
  } catch (e) {
    return { ok: false, mode: "rows", explanation: "", sql: "", error: (e as Error).message || "AI request failed" };
  }

  const explanation = (spec.explanation ?? "").trim();
  // off-topic / not-a-database request: decline gracefully instead of fabricating a query
  if (spec.mode === "reject") {
    return {
      ok: true, mode: "reject", sql: "",
      explanation: explanation || "I only search the royalty database — try assets, holders, rates, jurisdictions, or an aggregate like “top holders by number of royalties”.",
    };
  }
  const mode = spec.mode === "table" ? "table" : "rows";
  const check = validateSelect(spec.sql ?? "");
  if (!check.ok) {
    return { ok: false, mode, explanation, sql: (spec.sql ?? "").trim(), error: check.error };
  }

  let out: { fields: string[]; rows: Record<string, unknown>[] };
  try {
    out = await queryReadOnly(check.sql);
  } catch (e) {
    const msg = (e as Error).message || "query failed";
    return { ok: false, mode, explanation, sql: check.sql, error: msg.replace(/^error:\s*/i, "") };
  }

  if (mode === "rows") {
    const ids = out.rows.slice(0, 3000).map((r) => String(r.id ?? r[out.fields[0]]));
    return { ok: true, mode: "rows", explanation, sql: check.sql, ids };
  }
  const rows = out.rows.slice(0, 200).map((r) => out.fields.map((f) => cell(r[f])));
  return { ok: true, mode: "table", explanation, sql: check.sql, fields: out.fields, rows };
}

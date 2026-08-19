"use server";

import Anthropic from "@anthropic-ai/sdk";
import { query } from "@/lib/db";

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
       reviewed_by = 'local', reviewed_at = now()
     where id = $1::bigint`,
    [id, p.status ?? null, p.tier ?? null, p.keep ?? null,
     p.score_project_quality ?? null, p.score_instrument_quality ?? null,
     p.score_confidence ?? null, p.score_actionable ?? null, p.comments ?? null,
     p.rank ?? null, p.link ?? null],
  );
  return { ok: true };
}

// ── AI natural-language filter ────────────────────────────────────────────────
// The analyst types plain English ("producing gold in Nevada, available NSR under 2%").
// Claude turns it into a *whitelisted, structured* filter spec; we compile that spec to a
// parameterized read-only SELECT. The model's text never reaches SQL directly — only
// (field, op) pairs looked up in the tables below, and values passed as bound parameters.

type FieldKind = "text" | "commodity" | "num" | "avail" | "status" | "bool" | "present";

// field name (what Claude may emit) -> real column + how to compile it + a human label
const FIELDS: Record<string, { col: string; kind: FieldKind; label: string }> = {
  operator: { col: "operator", kind: "text", label: "operator" },
  jurisdiction: { col: "jurisdiction", kind: "text", label: "jurisdiction" },
  holder: { col: "holder", kind: "text", label: "held by" },
  project: { col: "project_name", kind: "text", label: "asset" },
  stage: { col: "stage", kind: "text", label: "stage" },
  royalty_type: { col: "royalty_type", kind: "text", label: "type" },
  regime: { col: "regime", kind: "text", label: "regime" },
  commodity: { col: "commodity", kind: "commodity", label: "commodity" },
  rate_pct: { col: "rate_pct", kind: "num", label: "rate" },
  royalty_available: { col: "royalty_available", kind: "avail", label: "availability" },
  status: { col: "status", kind: "status", label: "review status" },
  quote_verified: { col: "quote_verified", kind: "bool", label: "source-verified" },
  partial_coverage: { col: "partial_coverage", kind: "bool", label: "partial coverage" },
  rofr: { col: "rofr", kind: "bool", label: "ROFR" },
  buyback: { col: "buyback", kind: "present", label: "buyback" },
  step_down: { col: "step_down", kind: "present", label: "step-down" },
  production_cap: { col: "production_cap", kind: "present", label: "production cap" },
  production_threshold: { col: "production_threshold", kind: "present", label: "production threshold" },
  advance_payments: { col: "advance_payments", kind: "present", label: "advance payments" },
};
const OPS = ["contains", "eq", "lt", "lte", "gt", "gte", "has", "is", "present", "absent"] as const;
const AVAIL = ["available", "partial", "held", "unknown"];
const STATUS = ["pending", "validated", "rejected", "needs_info"];
const NUM_OP: Record<string, string> = { lt: "<", lte: "<=", gt: ">", gte: ">=", eq: "=" };

interface Condition { field: string; op: string; value: string }

// One condition -> a bound SQL fragment + a human chip, or null if it doesn't validate.
function buildFrag(c: Condition, params: unknown[]): { sql: string; chip: string } | null {
  const f = FIELDS[c.field];
  if (!f) return null;
  const v = (c.value ?? "").trim();
  const bind = (val: unknown) => { params.push(val); return `$${params.length}`; };
  switch (f.kind) {
    case "text": {
      if (!v) return null;
      if (c.op === "eq") return { sql: `lower(${f.col}) = lower(${bind(v)})`, chip: `${f.label}: ${v}` };
      return { sql: `${f.col} ILIKE ${bind(`%${v}%`)}`, chip: `${f.label}: ${v}` };
    }
    case "commodity": {
      const arr = v.split(/[,/&]|\bor\b|\band\b/).map((s) => s.trim()).filter(Boolean);
      if (!arr.length) return null;
      return { sql: `commodity && ${bind(arr)}::text[]`, chip: `commodity: ${arr.join("/")}` };
    }
    case "num": {
      const n = parseFloat(v.replace("%", ""));
      if (Number.isNaN(n)) return null;
      const opsql = NUM_OP[c.op] ?? "=";
      return { sql: `rate_pct ${opsql} ${bind(n)}`, chip: `rate ${opsql} ${n}%` };
    }
    case "avail": {
      const val = v.toLowerCase();
      if (!AVAIL.includes(val)) return null;
      return { sql: `royalty_available = ${bind(val)}::availability`, chip: `availability: ${val}` };
    }
    case "status": {
      const val = v.toLowerCase();
      if (!STATUS.includes(val)) return null;
      return { sql: `status = ${bind(val)}::review_status`, chip: `status: ${val}` };
    }
    case "bool": {
      const tv = ["true", "yes", "1"].includes(v.toLowerCase());
      return { sql: `${f.col} = ${bind(tv)}`, chip: tv ? f.label : `not ${f.label}` };
    }
    case "present": {
      if (c.op === "absent") return { sql: `${f.col} IS NULL`, chip: `no ${f.label}` };
      return { sql: `${f.col} IS NOT NULL`, chip: `has ${f.label}` };
    }
  }
}

const AI_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["interpretation", "conditions"],
  properties: {
    interpretation: {
      type: "string",
      description: "One short sentence restating what you understood the user to be asking for.",
    },
    conditions: {
      type: "array",
      description: "The filters to apply, combined with AND. Empty if the request cannot be mapped to the fields below.",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["field", "op", "value"],
        properties: {
          field: { type: "string", enum: Object.keys(FIELDS) },
          op: { type: "string", enum: [...OPS] },
          value: { type: "string", description: "Bound as a parameter. '' for present/absent ops." },
        },
      },
    },
  },
};

const AI_SYSTEM = `You translate a mining-royalty analyst's plain-English request into a structured filter over a database of third-party royalties found in technical reports. Return ONLY the filter spec.

Each row is one royalty on a mining asset. Fields you may filter on (use these exact field names and ops):

TEXT (op "contains" for partial match, "eq" for exact):
- operator: the company operating the asset
- jurisdiction: country / state / province (e.g. Nevada, Canada, Australia, Quebec)
- holder: the party that owns/receives the royalty (the counterparty)
- project: the asset/project name
- stage: exploration | PEA | PFS | FS | development | producing (use "contains")
- royalty_type: NSR | GSR | NPI | GVR | stream
- regime: NI 43-101 | S-K 1300 | JORC

commodity (op "has"): value is one or more metal SYMBOLS. Convert names to symbols:
gold=Au, silver=Ag, copper=Cu, nickel=Ni, zinc=Zn, molybdenum=Mo, platinum-group=PGE.
Multiple metals -> comma-separate them in one condition (e.g. "Au,Cu").

rate_pct (ops lt/lte/gt/gte/eq): the royalty percentage as a number. "under 2%" -> op "lt" value "2". "at least 1.5%" -> op "gte" value "1.5".

royalty_available (op "is", value one of available|partial|held|unknown): whether the royalty can be acquired. "available to buy / acquirable / for sale" -> "available".

status (op "is", value one of pending|validated|rejected|needs_info): the analyst review status.

BOOLEAN (op "is", value "true" or "false"):
- quote_verified: the royalty is verified against the source sentence
- partial_coverage: the royalty burdens only part of the property
- rofr: a right of first refusal / offer is attached

CLAUSE PRESENCE (op "present" or "absent", value ""):
- buyback: a buy-down / buy-back clause exists
- step_down: a sliding-scale / step-down structure exists
- production_cap: the royalty is capped
- production_threshold: payable only above a production threshold
- advance_payments: advance minimum royalty payments exist

Rules:
- Only use the fields and ops above. Combine conditions with AND.
- Convert metal names to symbols; put percentage thresholds on rate_pct.
- If part of the request maps and part doesn't, include what maps and note the rest in interpretation.
- If nothing maps, return an empty conditions array and explain in interpretation.`;

export interface AiSearchResult {
  ok: boolean;
  ids: string[] | null; // null = no structured filter produced (caller falls back to keyword)
  interpretation: string;
  chips: string[];
  count: number;
  error?: string;
}

/** Turn a natural-language query into matching royalty ids via Claude -> whitelisted SQL. Read-only. */
export async function aiSearch(nl: string): Promise<AiSearchResult> {
  const q = (nl ?? "").trim().slice(0, 400);
  if (!q) return { ok: true, ids: null, interpretation: "", chips: [], count: 0 };

  let spec: { interpretation?: string; conditions?: Condition[] };
  try {
    const client = new Anthropic();
    // Structured output (JSON schema) — a small, literal extraction; low effort keeps the box snappy.
    const res = (await client.messages.create({
      model: "claude-opus-5",
      max_tokens: 1024,
      output_config: { effort: "low", format: { type: "json_schema", schema: AI_SCHEMA } },
      system: AI_SYSTEM,
      messages: [{ role: "user", content: q }],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any)) as { content: { type: string; text?: string }[] };
    const text = res.content.find((b) => b.type === "text")?.text ?? "{}";
    spec = JSON.parse(text);
  } catch (e) {
    return { ok: false, ids: null, interpretation: "", chips: [], count: 0, error: (e as Error).message ?? "AI request failed" };
  }

  const interpretation = (spec.interpretation ?? "").trim();
  const conds = Array.isArray(spec.conditions) ? spec.conditions : [];
  const params: unknown[] = [];
  const frags: string[] = [];
  const chips: string[] = [];
  for (const c of conds) {
    const built = buildFrag(c, params);
    if (built) { frags.push(built.sql); chips.push(built.chip); }
  }
  if (!frags.length) {
    return { ok: true, ids: null, interpretation: interpretation || `Couldn’t turn “${q}” into filters.`, chips: [], count: 0 };
  }

  const sql = `select id::text as id from royalties where is_primary and ${frags.join(" and ")} limit 3000`;
  const rows = await query<{ id: string }>(sql, params);
  return { ok: true, ids: rows.map((r) => r.id), interpretation, chips, count: rows.length };
}

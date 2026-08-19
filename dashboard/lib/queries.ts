import "server-only";
import { query } from "@/lib/db";

// One royalty row as the LODE dashboard needs it (mirrors the `royalties` table).
export interface Royalty {
  id: string;
  asset: string;
  operator: string | null;
  juris: string | null;
  commodity: string[];
  stage: string | null;
  rate: string | null;
  rate_pct: number | null;
  type: string | null;
  holder: string | null;
  holder_note: string | null;
  avail: string; // royalty_available enum
  conf: number | null;
  // structured features (Claude-extracted)
  partial_coverage: boolean | null;
  advance_payments: string | null;
  production_threshold: string | null;
  production_cap: string | null;
  buyback: string | null;
  step_down: string | null;
  rofr: boolean | null;
  features_note: string | null;
  // provenance
  regime: string | null;
  source_label: string | null;
  source_url: string | null;
  quote: string | null;
  quote_verified: boolean;
  status: string;
}

const COLS = `id::text as id, project_name as asset, operator, jurisdiction as juris, commodity,
  stage, rate, rate_pct::float8 as rate_pct, royalty_type as type, holder, holder_note,
  royalty_available::text as avail, extract_confidence as conf,
  partial_coverage, advance_payments, production_threshold, production_cap, buyback, step_down, rofr, features_note,
  regime, source_label, source_url, source_quote as quote, quote_verified, status::text as status`;

/** Primary royalties (one per asset-royalty after dedup), available-first then by rate. */
export function getRoyalties(limit = 1500): Promise<Royalty[]> {
  return query<Royalty>(
    `select ${COLS} from royalties where is_primary
     order by (royalty_available='available') desc, rate_pct desc nulls last, asset
     limit $1`,
    [limit],
  );
}

export interface Kpis {
  royalties: number;
  assets: number;
  pending: number;
  verified_pct: number;
}

export async function getKpis(): Promise<Kpis> {
  const rows = await query<Kpis>(
    `select count(*)::int as royalties,
            count(distinct project_name)::int as assets,
            count(*) filter (where status='pending')::int as pending,
            round(100.0 * count(*) filter (where quote_verified) / greatest(count(*),1))::int as verified_pct
     from royalties where is_primary`,
  );
  return rows[0];
}

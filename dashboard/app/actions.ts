"use server";

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
}

/** Persist an analyst's review of one royalty. Nothing is "committed" until this runs (Matt's rule). */
export async function saveReview(id: string, p: ReviewPatch): Promise<{ ok: boolean }> {
  await query(
    `update royalties set
       status = coalesce($2::review_status, status),
       tier = $3, keep = $4,
       score_project_quality = $5, score_instrument_quality = $6,
       score_confidence = $7, score_actionable = $8,
       comments = $9,
       reviewed_by = 'local', reviewed_at = now()
     where id = $1::bigint`,
    [id, p.status ?? null, p.tier ?? null, p.keep ?? null,
     p.score_project_quality ?? null, p.score_instrument_quality ?? null,
     p.score_confidence ?? null, p.score_actionable ?? null, p.comments ?? null],
  );
  return { ok: true };
}

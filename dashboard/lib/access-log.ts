import "server-only";

import { query } from "@/lib/db";

/**
 * In-house usage analytics: one row per authenticated page view.
 *
 * LODE has no analytics and nobody is using it yet, so the first question worth answering is simply
 * "is anyone opening this, and who." Mirrors MarketWatch's access_log rather than inventing a second
 * shape — see its db/migrations/014_access_log.sql.
 *
 * The table is created lazily on first write for the same reason MarketWatch's is: the production
 * database is only reachable through Azure Cloud Shell (port 5432 is blocked from the office), so a
 * migration cannot be run against it from a dev machine. db/migrations/006_access_log.sql is the
 * canonical record of the shape.
 */
let ensured: Promise<void> | null = null;

function ensureTable(): Promise<void> {
  if (!ensured) {
    ensured = query(`
      create table if not exists access_log (
        id    bigint generated always as identity primary key,
        email text        not null,
        name  text,
        path  text        not null,
        at    timestamptz not null default now()
      )`)
      .then(() => query(
        "create index if not exists idx_access_log_email_at on access_log (email, at desc)"))
      .then(() => query(
        "create index if not exists idx_access_log_at on access_log (at desc)"))
      .then(() => undefined)
      .catch((e) => { ensured = null; throw e; });   // retry on the next request
  }
  return ensured;
}

/** Record one page view. Best-effort: analytics must never break a render. */
export async function recordAccess(email: string, name: string | null, path: string): Promise<void> {
  try {
    await ensureTable();
    await query("insert into access_log (email, name, path) values ($1, $2, $3)", [email, name, path]);
  } catch (e) {
    console.error("access-log write failed:", e);   // surfaced, not thrown
  }
}

/** Empty results rather than an error when the table does not exist yet (nobody has visited). */
async function safe<T>(sql: string, params: unknown[] = []): Promise<T[]> {
  try {
    return await query<T>(sql, params);
  } catch {
    return [];
  }
}

export interface UsageSummary {
  views: number;
  viewers: number;
  views_7d: number;
  viewers_7d: number;
  first_at: string | null;
  last_at: string | null;
}

export async function getUsageSummary(): Promise<UsageSummary> {
  const rows = await safe<UsageSummary>(`
    select count(*)::int                                                        as views,
           count(distinct email)::int                                           as viewers,
           count(*) filter (where at > now() - interval '7 days')::int           as views_7d,
           count(distinct email) filter (where at > now() - interval '7 days')::int as viewers_7d,
           min(at)::text                                                        as first_at,
           max(at)::text                                                        as last_at
      from access_log`);
  return rows[0] ?? { views: 0, viewers: 0, views_7d: 0, viewers_7d: 0, first_at: null, last_at: null };
}

export interface ViewerActivity {
  email: string;
  name: string | null;
  views: number;
  days_active: number;
  first_at: string;
  last_at: string;
}

export async function getViewerActivity(): Promise<ViewerActivity[]> {
  return safe<ViewerActivity>(`
    select email,
           max(name)                               as name,
           count(*)::int                           as views,
           count(distinct at::date)::int           as days_active,
           min(at)::text                           as first_at,
           max(at)::text                           as last_at
      from access_log
     group by email
     order by max(at) desc`);
}

export interface DailyViews { day: string; views: number; viewers: number }

export async function getDailyViews(days = 21): Promise<DailyViews[]> {
  return safe<DailyViews>(`
    select to_char(at::date, 'YYYY-MM-DD')  as day,
           count(*)::int                    as views,
           count(distinct email)::int       as viewers
      from access_log
     where at > now() - ($1 || ' days')::interval
     group by at::date
     order by at::date`, [days]);
}

export interface PathViews { path: string; views: number }

export async function getPathViews(): Promise<PathViews[]> {
  return safe<PathViews>(`
    select path, count(*)::int as views
      from access_log group by path order by count(*) desc`);
}

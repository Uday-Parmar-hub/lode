import { Pool } from "pg";

// Server-only Postgres pool for LODE. Reused across dev hot-reloads via a global so we don't leak a
// new pool per module re-eval. Reads DATABASE_URL (dashboard/.env.local) — the local conda Postgres.
const globalForPg = globalThis as unknown as { _lodePool?: Pool };

export const pool: Pool =
  globalForPg._lodePool ??
  new Pool({ connectionString: process.env.DATABASE_URL, max: 5 });

if (process.env.NODE_ENV !== "production") globalForPg._lodePool = pool;

export async function query<T = Record<string, unknown>>(
  text: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await pool.query(text, params);
  return result.rows as T[];
}

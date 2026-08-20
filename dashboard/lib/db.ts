import { Pool } from "pg";

// Server-only Postgres pool for LODE. Reused across dev hot-reloads via a global so we don't leak a
// new pool per module re-eval. Reads DATABASE_URL (dashboard/.env.local) — the local conda Postgres.
const globalForPg = globalThis as unknown as { _lodePool?: Pool; _lodePoolRO?: Pool };

export const pool: Pool =
  globalForPg._lodePool ??
  new Pool({ connectionString: process.env.DATABASE_URL, max: 5 });

// Separate pool on the SELECT-only `lode_ro` role for the AI text-to-SQL path — LLM-generated queries
// never touch the owner connection. The role also forces read-only txns + a statement timeout server-side.
export const poolRO: Pool =
  globalForPg._lodePoolRO ??
  new Pool({ connectionString: process.env.DATABASE_URL_RO ?? process.env.DATABASE_URL, max: 3 });

if (process.env.NODE_ENV !== "production") {
  globalForPg._lodePool = pool;
  globalForPg._lodePoolRO = poolRO;
}

export async function query<T = Record<string, unknown>>(
  text: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await pool.query(text, params);
  return result.rows as T[];
}

/** Run one LLM-generated SELECT on the read-only pool inside an explicit read-only transaction.
 *  Returns the column names (in order) and rows. Belt to the role's braces. */
export async function queryReadOnly(
  text: string,
): Promise<{ fields: string[]; rows: Record<string, unknown>[] }> {
  const client = await poolRO.connect();
  try {
    await client.query("begin transaction read only");
    await client.query("set local statement_timeout = 5000");
    const result = await client.query(text);
    return { fields: result.fields.map((f) => f.name), rows: result.rows as Record<string, unknown>[] };
  } finally {
    await client.query("rollback").catch(() => {});
    client.release();
  }
}

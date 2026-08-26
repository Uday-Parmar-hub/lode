import { Pool, type PoolClient } from "pg";

// Server-only Postgres pool for LODE. Reused across dev hot-reloads via a global so we don't leak a
// new pool per module re-eval. Reads DATABASE_URL (dashboard/.env.local) — the local conda Postgres.
const globalForPg = globalThis as unknown as { _lodePool?: Pool; _lodePoolRO?: Pool };

// Azure Database for PostgreSQL requires TLS (connection strings carry `?sslmode=require`). node-pg needs
// the ssl option set explicitly; rejectUnauthorized:false accepts Azure's managed cert. Local Postgres has
// no sslmode in its URL, so ssl stays off and nothing changes for local dev.
const sslFor = (url?: string): { rejectUnauthorized: boolean } | undefined =>
  url && url.includes("sslmode=require") ? { rejectUnauthorized: false } : undefined;

export const pool: Pool =
  globalForPg._lodePool ??
  new Pool({ connectionString: process.env.DATABASE_URL, max: 5, ssl: sslFor(process.env.DATABASE_URL) });

// Separate pool on the SELECT-only `lode_ro` role for the AI text-to-SQL path — LLM-generated queries
// never touch the owner connection. The role also forces read-only txns + a statement timeout server-side.
const roUrl = process.env.DATABASE_URL_RO ?? process.env.DATABASE_URL;
export const poolRO: Pool =
  globalForPg._lodePoolRO ??
  new Pool({ connectionString: roUrl, max: 3, ssl: sslFor(roUrl) });

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

/** Run `fn` inside a single write transaction on the owner pool (commit on success, rollback on throw).
 *  Used by the memory-chain append (insert a new version + demote the old one atomically). */
export async function withTransaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("begin");
    const result = await fn(client);
    await client.query("commit");
    return result;
  } catch (e) {
    await client.query("rollback").catch(() => {});
    throw e;
  } finally {
    client.release();
  }
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

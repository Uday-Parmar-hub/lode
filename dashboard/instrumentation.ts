/**
 * Startup schema check.
 *
 * Next.js runs register() once when the server boots. We use it to confirm the database actually has
 * the columns the queries need, and to say plainly which migration is missing if it does not.
 *
 * This exists because of a real outage: production's schema had drifted four migrations behind the
 * code, and nothing noticed until a deploy shipped a query selecting `is_producing`. The dashboard
 * then failed mid-render with a raw `column "is_producing" does not exist`, which tells you nothing
 * about what to do. The drift is easy to create: `az acr build` ships the local working tree, while
 * migrations have to be run by hand in Azure Cloud Shell (port 5432 is blocked from the office), so
 * "I wrote a migration" and "it ran in prod" are two unconnected steps.
 *
 * Deliberately non-fatal. A dashboard that boots and shows an error is more useful than one that
 * refuses to start, and the log line is what matters — it names the fix.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;

  // column -> the migration that adds it
  const REQUIRED: Record<string, string> = {
    country: "001_jurisdiction_fields",
    state_province: "001_jurisdiction_fields",
    continent: "001_jurisdiction_fields",
    jurisdiction_tier: "001_jurisdiction_fields",
    competitor_holder: "002_competitor_flag",
    dup_key: "003 (or scripts/dedupe.py, which creates it)",
    instrument_id: "003_memory_chain_foundation",
    origin: "003_memory_chain_foundation",
    needs_revalidation: "003_memory_chain_foundation",
    is_producing: "004_producing_flag",
  };

  try {
    const { query } = await import("@/lib/db");
    const rows = await query<{ column_name: string }>(
      "select column_name from information_schema.columns where table_name = 'royalties'",
    );
    const present = new Set(rows.map((r) => r.column_name));
    const missing = Object.keys(REQUIRED).filter((c) => !present.has(c));

    if (!missing.length) {
      console.log(`[schema] royalties: all ${Object.keys(REQUIRED).length} required columns present`);
      return;
    }
    console.error(
      "[schema] DATABASE IS BEHIND THE CODE — the dashboard will fail on these queries.\n" +
      missing.map((c) => `  missing column '${c}'  ->  db/migrations/${REQUIRED[c]}`).join("\n") +
      "\n  Migrations run in Azure Cloud Shell; see DEPLOY.md (the connection string can be read\n" +
      "  from the container app's 'database-url' secret, so no password is needed).",
    );
  } catch (e) {
    // Never block startup on the check itself — a database blip must not stop the app booting.
    console.error("[schema] check skipped:", e instanceof Error ? e.message : e);
  }
}

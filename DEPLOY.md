# Deploy — LODE (Azure)

Quick reference for pushing LODE to production. As-shipped detail + first-deploy commands live in
`scripts/deploy_azure.md`; design decisions in `CLAUDE.md`.

LODE runs as **one** deployed service — the dashboard. Extraction/dedup/enrichment are run **manually**
from `scripts/` (not a 24/7 poller like MarketWatch); the DB is loaded via Cloud Shell.

- **App:** `lode-dashboard` (Azure Container Apps, RG `RG-Marketwatch`, env `mw-env` — shared with MarketWatch).
- **URL:** https://lode-dashboard.greenmeadow-9faacea3.canadacentral.azurecontainerapps.io/ — gated by
  **basic auth** (`LODE_BASIC_AUTH` env secret, `matt:<preview-password>`).
- **DB:** Azure Postgres Flexible Server `lode-pg-orr`, database `lode`, admin `lodeadmin`. Port 5432 is
  blocked on the corp network → DB access is **Cloud Shell only**.
- **Registry:** `ormwacr01`.

## Deploy the dashboard (run from `dashboard/`; bump `vN` each time)

Current live: `v5`.

> ### ⚠ Check the schema BEFORE you deploy
>
> `az acr build` ships your **local working tree**, so a deploy carries every query written since the
> last one — including ones that need columns a migration added. Migrations do **not** run themselves:
> port 5432 is blocked from the office, so they are a manual Cloud Shell step, and "I wrote a
> migration" and "it ran in prod" are two unconnected things.
>
> This has already bitten once. Production sat **four migrations behind** the code; deploying picked
> up a query selecting `is_producing`, and the dashboard failed mid-render with
> `column "is_producing" does not exist`. Rolled back to the previous tag, applied the migrations,
> redeployed.
>
> In **Azure Cloud Shell** — no repo clone and no password needed, the connection string lives in the
> container app's secret:
>
> ```bash
> CONN=$(az containerapp secret show -g RG-Marketwatch -n lode-dashboard \
>          --secret-name database-url --query value -o tsv)
>
> psql "$CONN" -c "select column_name from information_schema.columns
>                   where table_name='royalties'
>                     and column_name in ('country','state_province','continent','jurisdiction_tier',
>                         'competitor_holder','dup_key','instrument_id','origin','needs_revalidation',
>                         'is_producing') order by 1"
> ```
>
> Expect **10 rows**. Anything missing: apply the matching `db/migrations/00N_*.sql` (all are additive
> and idempotent — safe to re-run, and their backfills are `WHERE ... IS NULL` so nothing is
> overwritten). **Do not apply `005`** — it is deliberately unapplied and refuses to run anyway.
>
> The app also checks this itself at startup now (`dashboard/instrumentation.ts`) and logs exactly
> which column and which migration are missing, so a drift shows up in
> `az containerapp logs show` rather than as a raw Postgres error in a user's face.

```bash
cd dashboard
az acr build --registry ormwacr01 --image lode-dashboard:v5 --file Dockerfile .
az containerapp update -g RG-Marketwatch -n lode-dashboard --image ormwacr01.azurecr.io/lode-dashboard:v5
```

- **Config only (no rebuild):** `az containerapp update … --set-env-vars KEY=value`
  (e.g. rotate the gate: `--set-env-vars LODE_BASIC_AUTH="matt:<new-pw>"`).
- **Verify:** `az containerapp logs show -g RG-Marketwatch -n lode-dashboard --tail 60`.

## Deploy the ingestion service (`lode-ingest`) — what the "Add to LODE" button calls

Built from the **repo root** (it needs `src/` and `scripts/`), unlike the dashboard which builds from
`dashboard/`:

```bash
az acr build --registry ormwacr01 --image lode-ingest:vN --file api/Dockerfile .
az containerapp update -g RG-Marketwatch -n lode-ingest --image ormwacr01.azurecr.io/lode-ingest:vN
```

> **The repo root needs `.dockerignore`, and it is load-bearing.** `az acr build .` uploads the whole
> context, and this repo carries an **8.4GB `corpus/`**. Without the ignore file the client hangs
> before the build is ever submitted — it does not error, it just sits there, and ACR shows no run at
> all. That is what a first attempt did. If a build appears to hang, check the context size before
> anything else.

Ingress is **internal** — reachable only from inside the `mw-env` Container Apps environment, never
from the internet. Authorisation additionally requires the caller to hold the `Royalties.Ingest` app
role, which MarketWatch's managed identity was granted.

Never set `LODE_INGEST_ALLOW_ANON` in production: it bypasses the role check, and the service would
then accept any caller that can reach it from inside the environment.

## Data (DB is loaded/updated out-of-band, via Cloud Shell)

The dashboard only *reads* the DB. To change the data you run the pipeline locally against a DB, then
get it into prod. Migrations + admin queries run in **Azure Cloud Shell** (5432 blocked otherwise):

```bash
export PGPASSWORD='<lodeadmin password — Container App db-url secret / vault>'
psql "host=lode-pg-orr.postgres.database.azure.com port=5432 dbname=lode user=lodeadmin sslmode=require" -f db/migrations/0NN_x.sql
```
(Migrations are additive; `db/schema.sql` is the source of truth — keep both in sync. When restoring a
dump into the v16 server, strip any v17-only `transaction_timeout` line first — see `scripts/deploy_azure.md`.)

## Gotchas

- `az acr build .` uploads your **local working tree** — a change can ship without being in git. Commit
  + push after deploying so the repo matches prod.
- The basic-auth gate is a **real credential** — keep it out of git (it lives in the `LODE_BASIC_AUTH`
  Container App env, not in any committed file). Rotate with `--set-env-vars` and tell Matt the new value.
- Extraction/dedup are **manual + reviewable-ledger** (see `CLAUDE.md`) — never push a data change the
  human hasn't validated.

## Migrating to the org (OR-Royalties-Inc) — ✅ DONE 2026-09-30

**`origin` is now `github.com/OR-Royalties-Inc/LODE`** (private), matching MarketWatch. The personal
remote is kept as `personal` → `github.com/Uday-Parmar-hub/lode`. All four branches were pushed:
`main`, `feat/marketwatch-lode-bridge`, `feat/memory-chain`, `experiment/web-mockup`.

The bridge work was deliberately left on its branch rather than merged to `main`: `main` is the
deployable line, and merging would imply a sign-off Elijah has not given. See
`docs/marketwatch_bridge.md`.

IT provisioned the repo with a placeholder README on an unrelated history, so `main` was force-pushed
over that single initial commit (nothing of value in it, and its README collided with the real one).

The pre-push audit below was re-run immediately before the push and was clean: 0 tracked confidential
files, no keys or gate password anywhere in history, 2.6M repo. Keep it here — it is worth re-running
before any future mirror to a new remote.

**Still open:** archive or delete the personal `Uday-Parmar-hub/lode` repo once the org copy is
confirmed, so OR's tooling lives only under the org. Nothing confidential is in it, but it is OR IP.

**1. Pre-push audit — re-run to confirm still clean (expect 0 / NONE):**
```bash
git ls-files | grep -cE '^data/|^corpus/|\.env$|\.xlsx$'                       # tracked confidential files → 0
git grep -InE 'sk-ant-[A-Za-z0-9]|matt:[A-Za-z0-9]' $(git rev-list --all) \
  | grep -vE 'matt:<|matt:\*\*\*'                                              # real keys / gate pw in history → none
du -sh .git                                                                    # small (~1-2M) — no corpus bloat
```
Confirmed clean 2026-09-09: nothing confidential is tracked or in history. The only literal credential is
the **local** dev default `lode:lode@localhost:5433` in `src/techreport/db.py` — correct to keep.

**2. Tidy the branch tree first (so the org gets a clean history):**
```bash
git checkout main && git merge --ff-only feat/memory-chain   # fold in the 24-peer competitor re-point
git branch -d master feat/royalty-db                         # stale / already-merged — safe to drop
# experiment/web-mockup has 2 unique commits — keep or drop, your call
```

**3. Mirror-push to the org (what was run):**
```bash
git remote add org https://github.com/OR-Royalties-Inc/lode.git   # exact name per Nelson
git push org main --tags
git remote set-url origin https://github.com/OR-Royalties-Inc/lode.git   # make the org the default
```

**4. After the org copy is confirmed:** archive/delete the personal `Uday-Parmar-hub/lode` repo, so OR's
tooling lives only under the org (nothing confidential is in it, but it's OR IP).

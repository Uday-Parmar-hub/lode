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

Current live: `v4`.

```bash
cd dashboard
az acr build --registry ormwacr01 --image lode-dashboard:v5 --file Dockerfile .
az containerapp update -g RG-Marketwatch -n lode-dashboard --image ormwacr01.azurecr.io/lode-dashboard:v5
```

- **Config only (no rebuild):** `az containerapp update … --set-env-vars KEY=value`
  (e.g. rotate the gate: `--set-env-vars LODE_BASIC_AUTH="matt:<new-pw>"`).
- **Verify:** `az containerapp logs show -g RG-Marketwatch -n lode-dashboard --tail 60`.

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

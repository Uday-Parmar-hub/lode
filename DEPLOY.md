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

## Migrating to the org (OR-Royalties-Inc) — ready to run when IT provisions the repo

LODE currently lives on a personal remote (`origin` → `github.com/Uday-Parmar-hub/lode`, private). The
end-state mirrors MarketWatch: a repo under **`OR-Royalties-Inc`**. **Blocked on IT (Nelson)** to create
the empty org repo + grant push access (confirm the repo name with him). It is a clean mirror-push
otherwise — no data or secrets have ever been committed.

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

**3. Mirror-push to the org (once the empty org repo exists):**
```bash
git remote add org https://github.com/OR-Royalties-Inc/lode.git   # exact name per Nelson
git push org main --tags
git remote set-url origin https://github.com/OR-Royalties-Inc/lode.git   # make the org the default
```

**4. After the org copy is confirmed:** archive/delete the personal `Uday-Parmar-hub/lode` repo, so OR's
tooling lives only under the org (nothing confidential is in it, but it's OR IP).

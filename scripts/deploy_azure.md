# LODE — Azure deploy runbook (feedback trial)

Goal: a protected, reachable instance with today's data for Matt to click. Reuses the MarketWatch
**subscription + resource group + Entra tenant**; LODE gets its **own** App Service (own URL) and its **own,
isolated** Postgres (kept away from the MarketWatch production DB). Auth = a Basic-auth password gate for the
trial (`LODE_BASIC_AUTH`); upgrade to Entra SSO later.

Run these in a terminal with the `!` prefix (interactive az login). Do one STEP at a time; paste the output back.

## Fill these in first
```sh
RG=RG-Marketwatch                 # confirm exact name: az group list -o table
LOC=canadacentral                 # match MarketWatch's region: az group show -n $RG --query location -o tsv
PG=lode-pg-$RANDOM                # new Postgres server name (must be globally unique)
APP=lode-dashboard                # web app name -> https://<APP>.azurewebsites.net (must be globally unique)
PLAN=lode-plan
PGADMIN=lodeadmin
PGADMINPW='<make-a-strong-password>'
ROPW='<make-a-strong-password-for-lode_ro>'
ANTHROPIC='<the ANTHROPIC_API_KEY value>'
GATE='matt:<pick-a-trial-password>'   # what Matt types to get in
```

## Step 0 — login + context
```sh
az login
az account show -o table          # confirm the right subscription
az group show -n $RG -o table     # confirm RG exists (reuse) — if not, we pick a different name
```

## Step 1 — Postgres (small burstable, own server)
```sh
az postgres flexible-server create \
  --resource-group $RG --name $PG --location $LOC \
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 \
  --version 16 --admin-user $PGADMIN --admin-password "$PGADMINPW" \
  --public-access 0.0.0.0 --yes
# --public-access 0.0.0.0 = allow other Azure services (App Service) to reach it.
az postgres flexible-server db create --resource-group $RG --server-name $PG --database-name lode
```
Capture the host: `PGHOST=$PG.postgres.database.azure.com`

## Step 2 — load the data + read-only role  (via Cloud Shell if corp net blocks 5432)
Upload `scratchpad/lode_dump.sql` and `scripts/pg_readonly.sql` to Cloud Shell, then:
```sh
# restore schema + data
psql "host=$PGHOST port=5432 dbname=lode user=$PGADMIN password=$PGADMINPW sslmode=require" -f lode_dump.sql
# create the SELECT-only role (edit the password in the file first, or run the grants inline)
psql "host=$PGHOST port=5432 dbname=lode user=$PGADMIN password=$PGADMINPW sslmode=require" <<SQL
DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lode_ro') THEN
  CREATE ROLE lode_ro LOGIN PASSWORD '$ROPW'; END IF; END \$\$;
REVOKE ALL ON DATABASE lode FROM lode_ro;
GRANT CONNECT ON DATABASE lode TO lode_ro;
GRANT USAGE ON SCHEMA public TO lode_ro;
GRANT SELECT ON royalties TO lode_ro;
ALTER ROLE lode_ro SET default_transaction_read_only = on;
ALTER ROLE lode_ro SET statement_timeout = '5000ms';
SQL
# sanity
psql "...user=$PGADMIN..." -c "select count(*) filter (where is_primary), count(*) from royalties"   # expect 709 / 1149
```

## Step 3 — App Service (Node 22 Linux)
```sh
az appservice plan create --resource-group $RG --name $PLAN --location $LOC --sku B1 --is-linux
az webapp create --resource-group $RG --plan $PLAN --name $APP --runtime "NODE:22-lts"
az webapp config set --resource-group $RG --name $APP --startup-file "node server.js"
```

## Step 4 — env vars
```sh
az webapp config appsettings set --resource-group $RG --name $APP --settings \
  DATABASE_URL="postgresql://$PGADMIN:$PGADMINPW@$PGHOST:5432/lode?sslmode=require" \
  DATABASE_URL_RO="postgresql://lode_ro:$ROPW@$PGHOST:5432/lode?sslmode=require" \
  ANTHROPIC_API_KEY="$ANTHROPIC" \
  LODE_BASIC_AUTH="$GATE" \
  HOSTNAME="0.0.0.0" \
  WEBSITE_NODE_DEFAULT_VERSION="22-lts"
```

## Step 5 — build + package (standalone) + deploy
Run locally in `dashboard/` (conda env prm_web):
```sh
npm run build
cp -r .next/static .next/standalone/.next/static
[ -d public ] && cp -r public .next/standalone/public
( cd .next/standalone && zip -qr ../../lode_app.zip . )
az webapp deploy --resource-group $RG --name $APP --src-path lode_app.zip --type zip
```

## Step 6 — smoke test
- Open `https://$APP.azurewebsites.net` → browser prompts for the Basic-auth login → enter `GATE`.
- Confirm KPIs (709 / 297 / 709 / 86%), click a row (Tasiast), run an AI query, try an aggregate.
- `az webapp log tail --resource-group $RG --name $APP` if anything 500s (usually a DB firewall or env-var typo).

## Hand to Matt
URL + the trial password (the part after `matt:` in `GATE`). Tell him it's a feedback build on today's pilot data.

## Later (post-trial): Entra SSO
Replace the password gate with App Service Easy Auth: new Entra app registration → redirect
`https://$APP.azurewebsites.net/.auth/login/aad/callback` → `az webapp auth` config. Remove `LODE_BASIC_AUTH`.

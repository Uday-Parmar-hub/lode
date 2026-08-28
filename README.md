# LODE — royalty origination from technical reports

**Find third-party royalties worth acquiring.** LODE reads OR Royalties' library of mining technical
reports (NI 43-101 / S-K 1300 / JORC), pulls out every **existing royalty or stream held by someone
other than the operator** — an encumbrance on the property, and therefore a potential acquisition
target — and turns them into a structured, filterable, source-verified database for the origination desk.

It's the sister project to **MarketWatch** (`~/projects/press_release_monitor`) — same stack, same
disciplines, different document. Where MarketWatch watches the newswire in real time, LODE mines the
deep technical-report corpus for royalties that already exist and could be bought.

> **Status: live preview.** ~**700 royalty instruments** across ~**300 assets**, extracted from the
> corpus, deduped, and served through a filterable + natural-language dashboard running on Azure
> (basic-auth preview for Matt). See `DEPLOY.md`.

## What it does

```
technical reports (corpus)                    ← archive_corpus.py  (Kscope SEDAR/EDGAR, ASX JORC, LSEG)
   │
   ▼  Claude extraction  (royalty.py, claude-sonnet-4-6)
extract every THIRD-PARTY royalty: type · rate · holder · verbatim quote · structured features
   │
   ▼  load  (load_royalties.py)
Postgres  (royalties: one row per royalty per source report)
   │
   ▼  resolve + dedup  (resolve_holders / resolve_assets / dedupe / audit_dupes_fable → apply_audit_fixes)
one row per real-world royalty — re-reports and spelling variants collapse, genuine stacks stay distinct
   │
   ▼  memory-chain  (migration 003)  — versioned instruments: stable instrument_id, origin, is_primary
   ▼  enrich  — jurisdiction tier (binary) · competitor-held flag · producing flag
   │
   ▼  dashboard  (Next.js / Tailwind)
filterable grid + a natural-language query ("producing gold in Nevada under 2%") + per-instrument
detail (verbatim source quote, feature checklist, version history) + analyst review / edit
```

Every royalty carries the **exact verbatim sentence** from the report that states it — nothing is a
paraphrase, so an analyst can verify each one against the source. The extraction is *assisted, not
blind*: the analyst confirms.

## The one principle: reviewable ledgers

Every time an LLM proposes a change — dedup merges, holder/asset resolution, jurisdiction, the
Fable-5 duplicate audit — it writes a **JSON ledger** a human reviews **before** anything touches the
DB, and the apply step is **non-destructive**: nothing is deleted; `is_primary` is a display flag; an
edit appends a new version and flags `needs_revalidation`. LLM proposes → human reviews → apply. That
discipline is why the numbers can be trusted and why a re-run never silently rewrites the desk's work.

## Stack

- **Python 3.11** (conda `mining_ai`) — extraction, resolution, dedup, enrichment
- **Postgres 16** — the store (`royalties` + memory-chain columns); `db/schema.sql` is the source of truth, changes via numbered `db/migrations/`
- **Anthropic SDK** — `claude-sonnet-4-6` for extraction, `claude-opus-5` for resolvers, `claude-fable-5` for the duplicate audit
- **PyMuPDF** — technical-report PDF text
- **Next.js 16 + Tailwind / `pg`** (conda `prm_web`) — the dashboard: direct Postgres reads + a guardrailed text-to-SQL query
- **Azure Container Apps** — hosting (mirrors MarketWatch)

## Setup (local)

```bash
conda activate mining_ai
# .env holds KSCOPE_API_KEY + ANTHROPIC_API_KEY (local only, gitignored)
# portfolio + competitor xlsx are read from $HOME (never committed)

scripts/pg_local.sh                      # local Postgres on :5433 (db=lode)
python scripts/init_db.py                # apply db/schema.sql
# corpus → extract → load → resolve → dedup → enrich  (see scripts/, run in that order)

cd dashboard && conda activate prm_web
npm run build && PORT=3010 npm run start  # production mode (dev HMR is flaky in WSL)
```

## Deploy

Live on Azure Container Apps (`lode-dashboard`). Build + push commands, DB access, and ops are in
**`DEPLOY.md`** (with the as-shipped log in `scripts/deploy_azure.md`).

## Layout

```
src/techreport/     extraction + pipeline
  royalty.py          Claude royalty extraction (the model + prompt)
  resolve.py          holder / asset / operator resolution (LLM-proposed ledgers)
  archive.py          corpus fetch/archive;  asx_jorc.py / edgar.py / lseg.py  source adapters
  portfolio.py        the OR portfolio (assets / operators / jurisdictions)
  db.py · config.py   connection + settings;  overrides.py  manual corrections
scripts/            the pipeline as runnable steps (load, resolve, dedupe, audit, enrich, backfill)
db/                 schema.sql (source of truth) + numbered migrations
dashboard/          Next.js app (app/board.tsx grid + detail, lib/queries.ts, app/actions.ts edits)
corpus/ · data/     downloaded reports + LLM ledgers — gitignored (big + confidential)
docs/               specs + Matt-feedback notes
```

## Confidentiality

The portfolio data, the competitor list, and the downloaded report corpus are **real OR Royalties
material**. `corpus/`, `data/`, `*.xlsx`, and `.env` are gitignored and never committed; the xlsx
inputs are read from `$HOME`. This repo is **private**. Review before pushing anywhere.

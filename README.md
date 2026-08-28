<div align="center">

<img src="docs/banner.svg" alt="LODE: royalty origination from technical reports" width="100%">

<br/>

*Reads the technical reports nobody has time to read. Finds every royalty already sitting on the property — held by someone else, and therefore for sale. Verifies each against the exact source sentence. Serves them to the origination desk, filterable and searchable in plain English.*

<br/>

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-16-000000?style=for-the-badge&logo=nextdotjs&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-Sonnet%20·%20Opus%20·%20Fable-D97757?style=for-the-badge&logo=anthropic&logoColor=white)
<br/>
![Status](https://img.shields.io/badge/status-LIVE%20on%20Azure-2ea043?style=for-the-badge)
![Scale](https://img.shields.io/badge/~700%20royalties-·%20~300%20assets-C6A15B?style=for-the-badge)
![License](https://img.shields.io/badge/license-internal-6e7681?style=for-the-badge)

</div>

---

## ⛏ The origination pipeline

A 250-page technical report becomes a structured, source-verified, deduplicated royalty on the desk's screen — every royalty carrying the exact sentence that proves it.

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'monospace','primaryColor':'#161b26','primaryTextColor':'#e6edf3','primaryBorderColor':'#C6A15B','lineColor':'#8a94a6','clusterBkg':'#0d1117','clusterBorder':'#2a3140'}}}%%
flowchart LR
    subgraph SRC["📑 CORPUS"]
        direction TB
        CA["Kscope SEDAR<br/>NI 43-101 · deep"]
        US["Kscope EDGAR<br/>S-K 1300"]
        AU["ASX / LSEG<br/>JORC"]
    end
    subgraph EX["🧠 EXTRACT"]
        direction TB
        RY["Claude · Sonnet<br/>third-party royalties"]
        Q["verbatim quote<br/>per royalty"]
    end
    subgraph RES["🧹 RESOLVE + DEDUP"]
        direction TB
        HR["holder · asset<br/>resolution"]
        DD["dedup + Fable audit<br/>precision-first"]
    end
    subgraph MEM["🧬 MEMORY + ENRICH"]
        direction TB
        MC["instrument_id<br/>versioned"]
        EN["jurisdiction · competitor<br/>producing"]
    end
    subgraph OUT["📊 SURFACE"]
        direction TB
        GRID["Dashboard<br/>filter + NL query"]
        DET["Detail + edit<br/>source-verified"]
    end

    CA --> RY
    US --> RY
    AU --> RY
    RY --> Q --> HR --> DD --> MC --> EN --> GRID
    EN --> DET

    classDef src fill:#12233a,stroke:#3b82f6,color:#dbeafe
    classDef ai fill:#2a1f12,stroke:#C6A15B,color:#f5e6c8
    classDef weave fill:#152114,stroke:#6a994e,color:#dcedc8
    classDef out fill:#241226,stroke:#a855f7,color:#ede0f5
    class CA,US,AU src
    class RY,Q ai
    class HR,DD,MC,EN weave
    class GRID,DET out
```

<div align="center"><sub><b>corpus → AI reads for royalties → resolve &amp; dedup → version &amp; enrich → surface, source-verified</b></sub></div>

---

## ✦ What it does

- **Finds royalties, not facts.** Claude reads each report for *existing third-party royalties* — an NSR, a stream, an AMR held by a party other than the operator. Those are encumbrances on the property, and therefore acquisition targets. This is origination, not a data-entry tracker.
- **Every royalty is source-verified.** Each one stores the **exact verbatim sentence** from the report — never a paraphrase — so an analyst confirms it against the source, not the model's word. "Instrument description, as stated in source document."
- **Structured features, not free text.** Rate, holder, type, plus a taxonomy — partial coverage, buy-down, sliding scale, **cap** (production *or* revenue threshold), advance payments, ROFR — each extracted as its own field.
- **One row per real royalty.** Precision-first dedup collapses the same royalty re-reported across years and spelling variants, while a genuine royalty *stack* (many holders on one asset) stays distinct. A Fable-5 audit surfaces the near-misses.
- **Remembers, never overwrites.** A memory-chain gives each royalty a stable `instrument_id` with append-only versions — an analyst edit becomes a new version flagged for re-validation, and the history stays visible.
- **Knows the ground.** Every instrument is tagged by jurisdiction tier (Tier-1 or not), whether the holder is a **competitor**, and whether the asset is **producing** — the exact filters an originator screens on.
- **Ask it in English.** "Producing gold in Nevada or Quebec under 2%, held by a competitor" → guardrailed text-to-SQL over the schema, right in the dashboard.

---

## 🛡 The one principle: reviewable ledgers

> Every time an LLM proposes a change — dedup merges, holder/asset resolution, jurisdiction, the Fable-5 audit — it writes a **JSON ledger a human reviews *before* anything touches the database.** The apply step is **non-destructive**: nothing is deleted, `is_primary` is a display flag, and an edit *appends* a version and flags `needs_revalidation`.
>
> **LLM proposes → human reviews → apply.** It's why the numbers can be trusted, and why re-running the pipeline never silently rewrites the desk's work.

---

## 🗄 Architecture

One core table, versioned and enriched. The dashboard only ever reads.

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'monospace','primaryColor':'#161b26','primaryTextColor':'#e6edf3','primaryBorderColor':'#C6A15B','lineColor':'#C6A15B'}}}%%
flowchart TB
    RAW["📥 EXTRACTED<br/>royalties — one row per royalty per source report"]
    DEDUP["🧹 RESOLVED<br/>dup_key · is_primary — one surfaced row per real royalty"]
    MEM["🧬 MEMORY-CHAIN<br/>instrument_id · origin · versions · needs_revalidation"]
    ENR["🧭 ENRICHED<br/>jurisdiction_tier · competitor_holder · is_producing"]
    DASH["📊 DASHBOARD<br/>grid · NL query · detail + edit"]
    RAW --> DEDUP --> MEM --> ENR --> DASH
    classDef layer fill:#161b26,stroke:#C6A15B,color:#f5e6c8
    class RAW,DEDUP,MEM,ENR,DASH layer
```

`db/schema.sql` is the source of truth; schema changes are numbered migrations in `db/migrations/` (001–004). Full rationale in [`CLAUDE.md`](CLAUDE.md).

---

## 🧰 The stack

| Layer | Choice |
|---|---|
| **Language** | Python 3.11 (conda `mining_ai`) |
| **Store** | PostgreSQL 16 · single source of truth · local `:5433` / Azure Flexible Server |
| **AI** | Anthropic SDK · **Sonnet 4.6** extraction · **Opus 5** resolvers · **Fable 5** duplicate audit |
| **Documents** | PyMuPDF (report PDF text) · vendored Kscope client · ASX / EDGAR / LSEG adapters |
| **Dashboard** | Next.js 16 · React 19 · Tailwind · `pg` · direct Postgres reads + guardrailed text-to-SQL |
| **Hosting** | Azure Container Apps (mirrors MarketWatch) |
| **Discipline** | reviewable JSON ledgers · non-destructive apply · verbatim source quote per royalty |

---

## 📈 By the numbers

<div align="center">

| | | |
|:--:|:--:|:--:|
| **~700** | **~300** | **1,150** |
| royalty instruments | assets covered | source rows |
| **3** | **4** | **LIVE** |
| Claude model tiers | schema migrations | on Azure |

</div>

---

## 🛰 Live on Azure

One deployed service — the dashboard reads live Postgres behind a preview gate. Extraction and dedup run on demand from `scripts/` under the reviewable-ledger discipline.

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'monospace','primaryColor':'#161b26','primaryTextColor':'#e6edf3','primaryBorderColor':'#C6A15B','lineColor':'#8a94a6','clusterBkg':'#0d1117','clusterBorder':'#2a3140'}}}%%
flowchart LR
    USER["👤 Matt / desk"] -->|basic-auth| DASH
    subgraph ACA["☁ Azure Container Apps"]
        DASH["lode-dashboard<br/>Next.js · HTTPS"]
    end
    DASH -->|reads| PG[("🐘 Postgres 16<br/>lode-pg-orr")]
    PIPE["💻 local pipeline<br/>extract · dedup · enrich"] -.->|Cloud Shell load| PG
    ACR["📦 ACR<br/>ormwacr01"] -.->|deploy| ACA
    classDef a fill:#12233a,stroke:#3b82f6,color:#dbeafe
    classDef b fill:#2a1f12,stroke:#C6A15B,color:#f5e6c8
    class USER,DASH b
    class PIPE,PG,ACR a
```

**The desk's toolkit** — all live in the dashboard:

| | Feature | What it does |
|:--:|---|---|
| 🔎 | **Natural-language query** | "Producing gold in Nevada under 2%" → guardrailed text-to-SQL over the schema. |
| 🎚 | **Screen filters** | Metal · regime (43-101 / S-K1300 / JORC) · region · **Tier-1** · **Producing** · **Competitor-held**. |
| 📜 | **Source-verified detail** | Every instrument shows its verbatim source sentence + a full feature checklist (dashes where absent, for a consistent read). |
| 🧬 | **Version history** | The memory-chain per instrument — every version, its origin (Claude / human-edited), and what needs re-validation. |
| ✏️ | **Analyst edit** | Correct a holder/rate/etc. in place; the edit appends a version and flags it, never overwriting the source. |

---

## 🚀 Run it

```bash
# Postgres (local, :5433, db=lode)
scripts/pg_local.sh
python scripts/init_db.py                       # apply db/schema.sql

# Dashboard (reads live Postgres) — production mode; dev HMR is flaky in WSL
cd dashboard && conda activate prm_web
npm run build && PORT=3010 npm run start          # → http://localhost:3010
```

<details>
<summary><b>First-time setup + the pipeline (corpus → dashboard)</b></summary>

```bash
conda activate mining_ai
pip install -e .
# .env holds KSCOPE_API_KEY + ANTHROPIC_API_KEY (local only, gitignored)
# portfolio + competitor xlsx are read from $HOME (never committed)

# Run the pipeline in order (each LLM step writes a reviewable ledger to data/ → review → apply):
python scripts/archive_corpus.py          # fetch technical reports → corpus/ + manifest
python scripts/royalty_pilot.py           # Claude extraction → extraction JSON
python scripts/load_royalties.py          # → royalties table
python scripts/resolve_holders.py         # → data/holder_merges.json   (review, then --apply)
python scripts/resolve_assets.py          # → data/asset_aliases.json   (review, then --apply)
python scripts/dedupe.py                  # is_primary + dup_key + instrument_id (re-runnable)
python scripts/audit_dupes_fable.py       # → data/audit_merges.json    (candidates → apply_audit_fixes.py)
python scripts/enrich_jurisdiction.py     # tier   (ledger → --apply)
python scripts/flag_competitors.py        # competitor-held (ledger → --apply)
python scripts/backfill_producing.py      # is_producing from stage (--apply)
```
</details>

Deploy commands + Cloud Shell DB ops: **[`DEPLOY.md`](DEPLOY.md)**.

---

## 🧭 Source reality

Honest about how far back each jurisdiction reaches — named, not papered over.

| Jurisdiction | Report | Source | History |
|---|---|---|:--:|
| Canada (~100) | NI 43-101 | Kscope SEDAR | ✅ deep |
| US (~30) | S-K 1300 | Kscope EDGAR | ✅ |
| Australia (~18) | JORC | ASX free API | ⚠️ forward-only |
| JSE / China / private | SAMREC / other / — | — | ❌ no auto-source |

The **AU-history gap** is the one place LSEG (ASX + history) or manual entry would return — a scoped gap, flagged to the desk.

---

## 🗺 Repo map

<details>
<summary><b>Expand</b></summary>

```
tech_report_db/
├── CLAUDE.md                  ← project decisions (read first)
├── README.md                  ← you are here
├── DEPLOY.md                  ← push-to-prod + DB ops
├── db/
│   ├── schema.sql             ← canonical DDL
│   └── migrations/            ← 001 jurisdiction · 002 competitor · 003 memory-chain · 004 producing
├── src/techreport/
│   ├── royalty.py             ← Claude royalty extraction (model + prompt)
│   ├── resolve.py             ← holder / asset / operator resolution
│   ├── archive.py             ← corpus fetch; asx_jorc · edgar · lseg adapters
│   ├── portfolio.py           ← the OR portfolio; overrides.py manual corrections
│   └── db.py · config.py
├── scripts/                   ← the pipeline as runnable steps + reviewable-ledger apply scripts
├── dashboard/                 ← Next.js: app/board.tsx (grid + detail) · lib/queries.ts · app/actions.ts (edits)
├── corpus/ · data/            ← reports + LLM ledgers — gitignored (big + confidential)
└── docs/                      ← specs + Matt-feedback notes
```
</details>

---

## ☁ What's next

- **Confirm the competitor list** with Matt, then re-run flagging against the current file.
- **Two new commodity-specific features + area-of-interest** — pending Matt's confirm + a re-extraction.
- **Memory-chain review** (`docs/specs/memory_chain.md`) + the 3 held dedup merges + null-holder attribution.
- **The parked tech-report tracker** (resources / reserves / economics) — still waiting on Matt's field list.
- **Migrate to the org** (`OR-Royalties-Inc`), like MarketWatch — a clean mirror-push from here.

---

<div align="center">
<sub><b>LODE</b> · royalty-origination tooling for OR Royalties · internal, not for distribution</sub>
</div>

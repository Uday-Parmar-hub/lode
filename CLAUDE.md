# Technical Report Database — Project Context (Tool 2)

> Read this first each session. It's the plan for a **structured, auto-filled database of OR
> Royalties' portfolio technical reports**, with a filterable dashboard. Sister project to
> MarketWatch (`~/projects/press_release_monitor`) and the impairment tool
> (`~/projects/impairment_analyzer`) — same stack, different document.

## What this is

Matt's ask: replace the Excel "tech report tracker" (goes stale, filtering too rigid/confusing)
with a real database of the **key data from each portfolio asset's technical reports**, that:

- **auto-fills from Kscope** (per operator) into fields **Matt will specify** (resources, reserves,
  LOM, production, AISC, capex, NPV/IRR, price deck, recovery, …) — *not finalized yet*;
- keeps **history** — as far back as we can go, per asset (assets get a new report every few years);
- has a **professional, filterable dashboard** — including **natural-language filtering**
  ("gold assets in Canada, reserves > 1 Moz, AISC < $1,200" → text-to-SQL over the schema);
- is likely **editable** (auto-fill is assisted, not blind — analyst verifies the numbers) — confirm;
- **stores + displays report figures** ("a particular chapter") **alongside** the data — *just show
  them, no vision analysis* — so an analyst can eyeball/compare;
- sets up **benchmarking** later (falls out for free once assets are structured + comparable).

## Scope / scale (from OR_Portfolio_List_2026-08-04.xlsx)

- **199 assets** = the folder/row count, but the fetch is **per-operator** (~142 distinct; ~100
  auto-fetchable — the rest are private or foreign). +10–15 more names coming from Matt.
- **Not one document type.** Canada → NI 43-101 (Kscope/SEDAR, deep history ~1997). US → S-K 1300
  (Kscope/EDGAR). Australia → **JORC** via ASX. JSE/China/private → other codes / none.

## The honest source reality (matters for "as far back as we can go")

| Jurisdiction | Report | Source | History |
|---|---|---|---|
| Canada (~100) | NI 43-101 | Kscope SEDAR | ✅ deep |
| US (~30) | S-K 1300 | Kscope EDGAR | ✅ |
| Australia (~18) | JORC | ASX free API | ⚠️ **forward-only** (5-item window, no backfill) |
| JSE / China / private | SAMREC / other / none | — | ❌ no auto-source |

**The AU-history gap is real**: the free ASX API can't backfill historical JORC reports — that's the
one spot where **LSEG** (ASX + history) or manual would come back in. Name it to Matt as a scoped gap
(auto CA/US history; AU forward-only or manual), don't pretend it's covered.

## Build order (corpus FIRST — it's schema-independent)

1. **[IN PROGRESS] Corpus layer** — fetch + archive every technical report per asset into a folder +
   a manifest (jsonl). This is the ONLY part that doesn't depend on Matt's field list, so it's the
   right thing to build now while he finalizes fields. Kscope-covered majority (CA + US ≈ 130 assets)
   first. **Validate first**: a Kscope depth-probe (does it return the *historical* 43-101s for a
   producing asset — how many, how far back).
2. **[BLOCKED on fields] Schema** — relational (asset → report → resource/reserve/economics rows);
   NOT flat, because resources/reserves are multi-category/multi-deposit.
3. **[BLOCKED on fields] Extraction** — Claude over the report PDF (targeted sections), **source-page
   citation per field** + human review/edit. Harder than MarketWatch (150–300pp dense tables).
4. **[LATER] Images** — PyMuPDF pulls figures from the named chapters; store + display, no inference.
5. **[LATER] Dashboard** — Next.js data-grid + NL filter (text-to-SQL, guardrailed) + per-asset
   detail view with the stored figures. Benchmarking = a view on top.

## Open questions for Matt (get these before schema/extraction)

1. **The exact field list** — drives everything.
2. **Editable? + audit trail** (auto-value vs human-corrected).
3. **Which chapters/figures** to store + show.
4. History depth confirmed (as far back as possible — yes).
5. **Document scope** — 43-101 only, or PEA/PFS/DFS + S-K 1300 + JORC studies?

## Stack (reuse — proven in the sister projects)

Python 3.11 (conda `mining_ai`), Kscope client **vendored** in `src/techreport/kscope_client/`
(don't hand-edit; re-vendor from MarketWatch), PyMuPDF for PDF text+images, Anthropic SDK for
extraction, Postgres + Next.js/Tailwind for the DB + dashboard, Azure for hosting.

## Confidentiality

Portfolio data is real. The xlsx is **not committed** (read from `$HOME`); the downloaded `corpus/`
and any `data/` are gitignored. Review before pushing anywhere.

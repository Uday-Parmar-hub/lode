# LODE — Project Context (Tool 2)

> **Read this first each session. Keep it accurate; update it when major decisions change.**

LODE is a **royalty-origination database**: it reads OR Royalties' library of mining technical reports
(NI 43-101 / S-K 1300 / JORC) and extracts every **existing third-party royalty or stream** — an
encumbrance held by someone *other* than the operator, i.e. a potential **acquisition target** — into a
structured, source-verified, filterable dashboard for the origination desk.

- **Internal name:** LODE / Tool 2. Repo: `~/projects/tech_report_db` (product name is LODE).
- **Sister projects:** MarketWatch / Tool 1 (`~/projects/press_release_monitor`, live newswire monitor)
  and the impairment fork. Same stack, same disciplines.
- **Primary user:** Matt (lead), origination desk. **Builder:** Uday (sole engineer).

## Current state (2026-08)

- **Live preview** on Azure Container Apps (`lode-dashboard`, basic-auth gate for Matt), same env as
  MarketWatch. See `DEPLOY.md`.
- ~**700 royalty instruments** / **1,150 rows** / ~**300 assets** in Postgres, extracted from the corpus,
  deduped, memory-chained, and enriched (jurisdiction / competitor / producing).
- Dashboard: filterable grid + natural-language query + per-instrument detail (verbatim quote, feature
  checklist, version history) + analyst review/edit.

### Origin note (don't get confused by old docs)

LODE began as a different ask — a **tech-report *tracker*** (resources / reserves / LOM / AISC / NPV per
Matt's field list). That tracker is **parked, still blocked on Matt's fields.** While waiting, the
corpus-first work turned into the **royalty-origination** tool, which became the actual shipped product.
When you see "resources/reserves/economics" language in old specs, that's the parked tracker, not LODE.

## Architecture

One core table, `royalties` (one row per royalty per source report), plus:
- **Memory-chain** (migration 003): `instrument_id` (stable identity for a real-world royalty across
  reports/versions), `origin` (`claude` / `claude_human_edited` / `human`), `is_primary` (the surfaced
  version), `needs_revalidation`, `dup_key`. Edits **append** a new version; nothing is overwritten.
- **Enrichment columns:** `country / state_province / continent / jurisdiction_tier` (001),
  `competitor_holder` (002), `is_producing` (004).

`db/schema.sql` is the **source of truth**; every schema change is a numbered migration in
`db/migrations/`. Prod migrations run via **Azure Cloud Shell** (5432 blocked on corp net).

## Pipeline (scripts, in order)

```
archive_corpus.py     fetch technical reports → local corpus + manifest (Kscope SEDAR/EDGAR, ASX JORC, LSEG)
royalty_pilot.py      run royalty.py (Claude, sonnet-4-6) over the corpus → extraction JSON
load_royalties.py     JSON → royalties table
resolve_holders.py    LLM-propose holder merges → data/holder_merges.json  (review → apply)
resolve_assets.py     LLM-propose asset aliases  → data/asset_aliases.json (review → apply)
dedupe.py             deterministic + ledger dedup → is_primary + dup_key + instrument_id (re-runnable)
audit_dupes_fable.py  Fable-5 duplicate audit → data/audit_merges.json  (candidates, not a score)
apply_audit_fixes.py  apply human-confirmed audit merges (dry-run default)
enrich_jurisdiction.py / flag_competitors.py / backfill_producing.py    enrichment (ledger + apply)
```

## Key technical decisions (do not relitigate)

1. **Reviewable ledgers.** Every LLM-proposed change (dedup, holder/asset resolution, jurisdiction, the
   Fable audit) writes a JSON ledger under `data/` a human reviews **before** apply. LLM proposes →
   human reviews → apply. Never commit a data change the human hasn't validated (Matt's rule).
2. **Non-destructive.** Nothing is deleted. `is_primary` is a display flag; an edit appends a new
   version (`origin='claude_human_edited'`) and sets `needs_revalidation`. A re-run of dedup reproduces
   state without clobbering confirmed merges (see the persistence step in `dedupe.py`).
3. **Verbatim quote per royalty.** Extraction always stores the exact source sentence (`quote`), never a
   paraphrase — the analyst verifies against it. "Instrument description, as stated in source document."
4. **Model tiers:** `claude-sonnet-4-6` extraction · `claude-opus-5` resolvers · `claude-fable-5` audit.
   `temperature` is **omitted** on opus-5/fable-5 (they 400 on it); Fable is non-deterministic → treat
   its audit output as *candidates*, not a score.
5. **Dedup is precision-first.** Distinct holders on one asset stay distinct rows (a genuine royalty
   *stack* is preserved); only re-reports + spelling variants of the same party/asset/type/rate collapse.
6. **Memory-chain, not rewrite.** `instrument_id` is the durable chain identity; versions are append-only;
   `is_primary` = newest/most-trustworthy shown. See `docs/specs/memory_chain.md`.
7. **Jurisdiction tier is binary** (Matt: no tier 2/3 list to maintain) — `1` = US/CA/AU, `NULL` = not.
8. **Producing is binary + point-in-time** (migration 004), kept *alongside* the free-text `stage`:
   Claude extracts `is_producing` for new reports; existing rows backfilled from `stage`.
9. **"cap" is general** — a cumulative cap (production volume OR revenue threshold) after which the
   royalty/stream stops; advance-payments and ROFR are kept as distinct features.
10. **Kscope v2 SEDAR uses the `page` param** (not `start`) to reach deep history (2013–2017); the
    v3 resolver is lossy. Client is **vendored** in `src/techreport/kscope_client/` — don't hand-edit.

## Source reality ("as far back as we can go")

| Jurisdiction | Report | Source | History |
|---|---|---|---|
| Canada (~100) | NI 43-101 | Kscope SEDAR | ✅ deep |
| US (~30) | S-K 1300 | Kscope EDGAR | ✅ |
| Australia (~18) | JORC | ASX free API | ⚠️ forward-only (no backfill) |
| JSE / China / private | SAMREC / other / none | — | ❌ no auto-source |

The **AU-history gap** is the one spot where LSEG (ASX + history) or manual would return — name it to
Matt as a scoped gap; don't pretend it's covered.

## Open items with Matt (as of 2026-08)

- **Competitor list** — confirm `OR_Competitor_List_2026-08-04.xlsx` is current before re-running flagging.
- **2 new commodity-specific features + area-of-interest** — needs his confirm + a re-extraction.
- **Memory-chain spec** review (`docs/specs/memory_chain.md`); **3 held dedup merges** (Cuiú Cuiú,
  Segilola, Eastmain); **null-holder** attribution; re-validate the ~10 flagged instruments.
- The parked **tech-report tracker** (resources/reserves) — still waiting on his field list.
- ⏳ **Matt is on paternity leave from ~mid-Sept** — batch these decisions to him before then.

## Stack

Python 3.11 (conda `mining_ai`) · Postgres 16 (local `:5433` db=lode via `scripts/pg_local.sh`;
Azure Flexible Server `lode-pg-orr` in prod) · Anthropic SDK · PyMuPDF · Next.js 16 + Tailwind + `pg`
(conda `prm_web`, run production mode — dev HMR is flaky in WSL) · Azure Container Apps.

## Confidentiality

Portfolio data, the competitor list, and the downloaded `corpus/` are **real OR material**. `corpus/`,
`data/`, `*.xlsx`, `.env` are gitignored and never committed; xlsx inputs are read from `$HOME`. The
repo is **private**. `ANTHROPIC_API_KEY` / DB creds never in git. Review before pushing anywhere.

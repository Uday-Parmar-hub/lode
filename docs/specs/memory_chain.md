# LODE — "Memory" / Instrument Version-Chain (design spec)

Status: **draft, in build (feat/memory-chain)** · Owner: Uday · From Matt's direction (2026-08-25), design delegated to us.
This doubles as the review doc for Matt/Elijah — react to the **Locked decisions** section.

## The problem
A single royalty (an "instrument") shows up across many technical reports over the years, and analysts also correct
what Claude extracted. Today an edit overwrites the row, and a new report can create a near-duplicate. We want a
**memory**: new information *appends* to an instrument's record (never silently overwrites), the newest view is what
you see, and the full history stays auditable — with human validation gating everything.

## Core model — instrument chains
Every real royalty is an **instrument** with a stable identity (`instrument_id`). Under it hangs a **chain of
versioned rows**, one per source event:
- a **report/PR** that mentions the royalty (NI 43-101, S-K 1300, or later a MarketWatch PR), or
- a **human edit** to an existing version.

Each version row carries: the field snapshot, `source` + `source_date`, `origin` (provenance), `status`
(validation), and `created_at`. **Append-only — rows are added, never overwritten.**

- **Display** = the newest version per instrument (grid shows one row per instrument, newest source on top).
- **History** = the older versions, shown in a per-instrument "version history" panel and findable in search.
- **One row per instrument** in the grid (not collapsed into the asset) — matches today's behaviour.

This is an **evolution of the existing dedup**, not a rewrite: today's `dup_key` groups a chain, `is_primary` flags
the shown version, non-primary rows are the history ("corroborated by N reports"). We formalise that into a stable
`instrument_id` + provenance + a re-validation lifecycle.

## Provenance (`origin`, per version row)
- `claude` — AI-extracted, unreviewed
- `claude_human_edited` — AI-extracted, a human then corrected a field
- `human` — fully human-entered (manual add)
- `marketwatch` — ingested from a PR via MarketWatch (Stage 2), validation-gated

## Validation lifecycle
- A new version (new source **or** human edit) sets the instrument to **needs re-validation** — a prior "validated"
  does not auto-carry to new data.
- The grid shows the newest version on top, **badged** "new source — needs re-validation" when the top version is
  unvalidated, so analysts see fresh data without trusting it blindly. (Reconciles "newest on top" with
  "validated takes priority": newest is *shown*, validated is *trusted*.)

## Completeness — do NOT assume the latest report has everything
Matt: the newest report *probably* restates all older royalties, "but not 100% sure." So we **never** render "just
the newest report's contents." The page is the **union of all known instruments**, each at its newest version. If a
new report omits an instrument an older one had, that instrument **persists**, flagged *"last seen in <report/date>
— not in the latest report"* (may have been bought out / not restated). This handles the uncertainty instead of
betting on it.

## Matching — the "memory" (how a new source finds its instrument)
For each royalty in a new source, decide: **same instrument** (append a version to its chain) or **new instrument**
(start a chain). Reuses the existing dedup matchers (asset + type + rate + holder identity, drift-aware,
precision-favouring — when unsure, keep distinct), run **incrementally per source** rather than as a batch.
- **AI proposes, human confirms** the link during validation (especially for MarketWatch/manual adds). The trusted
  43-101 batch keeps today's reviewed dedup.
- **Second-opinion audit:** a separate, independent model pass (Matt suggested **Fable 5**) periodically scans the
  DB for suspected duplicates the primary matcher missed → surfaced for human review, **never auto-merged**. Two
  independent passes catch different errors; model choice stays empirical (whichever flags more *real* dupes).

## Two build stages (Matt's staging)
- **Stage 1 — NI 43-101 history.** Formalise the technical-report backfill into instrument chains; newest report on
  top. (We already have this data.)
- **Stage 2 — MarketWatch / manual.** Add PRs and manual entries to the DB, provenance-tagged, validation-gated,
  appended to matching instrument chains (or new chains).

## Manual add (Stage 2 UX)
An **"Add royalty"** action: either pre-fill from a MarketWatch item (editable) or type from scratch → save as a
`human`/`marketwatch` version, matched to an existing instrument (AI-proposed, human-confirmed) or a new chain,
status = pending validation.

## Locked decisions (for Matt/Elijah to confirm)
1. **Append-only.** Edits and new sources add rows; nothing is overwritten; latest shown, history retained.
2. **Newest shown, validated trusted.** Top = newest version; badge it when unvalidated / needs re-validation.
3. **Conflicts:** newest wins for display; the change stays visible in history ("2% per 2018 → 1.5% per 2026").
4. **Matching:** AI proposes the link, human confirms; Fable-5 (or a second model) audits for missed dupes,
   never auto-merges. Omitted-by-latest instruments persist, flagged.

## Build increments (this branch)
1. **Foundation (additive migration):** `instrument_id` (stable, backfilled from current dup_key groups), `origin`,
   re-validation status. Non-destructive — the current tool keeps working. ← in progress
2. **Append-on-edit:** review-save writes a new version row (origin `claude_human_edited`, dated) instead of updating.
3. **History UI:** newest-on-top, per-instrument version panel, provenance badges, re-validation flag.
4. **Incremental matching + Fable-5 audit.**
5. **Manual add** flow.

## Safety / non-goals
- Additive migrations only; nothing deleted; the live tool is untouched until an increment is validated and deployed.
- No data change is committed to the DB without human validation (Matt's standing rule).
- Not in scope yet: web-search for current status (parked, decision D-1).

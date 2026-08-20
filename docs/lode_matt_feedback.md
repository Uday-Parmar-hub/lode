# LODE — Matt's feedback (2026-08-20, from the local demo + screenshots)

Status: **captured, not started.** Work begins 2026-08-21. Rule stands: never auto-commit data changes
without human validation. This is the roadmap for the part-time phase.

## The emerging data model (from Matt's nomenclature)
His renaming isn't cosmetic — it implies the real shape of the thing:
- **Property** — the underlying mine/asset (static facts: metals, jurisdiction)
- **Instrument** — a royalty / stream / offtake *on* a property (has a Holder)
- **Operator** — the property's owner/operator
- **Holder** — the entity that owns the instrument
- → Direction: **Property → Instrument(s) → a source-version chain.** Operator/Holder already match today;
  the renames are royalty→**instrument** and asset/project→**property**.

---

## A. Quick wins — start here
1. **Remove the Stage filter** (Producing / Development / Resource / Exploration). Matt's reasoning: a mine's
   status changes constantly, and a 10-year-old technical report can't tell you where a project is *today*.
   (Metal + jurisdiction never change; status does.) → drop the filter chips. See decision D-2 on the field.
2. **Add "Other" to the metal filter** + let extraction tag "Other" (U, Li, V, graphite, REE, etc.).
3. **Multiple metals per instrument** — ALREADY supported (`commodity` is an array). Just confirm to Matt; no work.
4. **Nomenclature relabel across the UI** — Royalties → **Instruments**, Asset/Project → **Property**
   (Operators & Holders already correct).
5. **Rename the "Verbatim from the technical report" panel** → **"Instrument description — text from source."**
   (Confirm this is what he meant by the last note next to that screenshot.)

## B. Data-model + extraction — medium
6. **Jurisdiction build-out** (he likes this axis): add
   - **state/province** — extract from the report, blank if N/A
   - **continent** — derive from country
   - **tier** — Tier-1 = USA/Canada/Australia; Tier 2/3 from a list **Matt will send** (chase him).
7. **Competitor flagging** — match each instrument's **Holder** against the competitor list
   (`~/OR_Competitor_List_2026-08-04.xlsx` — confirm it's the right one) → flag "owned by competitor" →
   force **not available** + **score 0**.
8. **Instrument features — show them ALL on the page, N/A when not relevant** (not just the ones present).
   Matt's taxonomy:
   - **Partial coverage** — Claude decides if it covers the whole property (or ≥ the economically interesting
     part); if in doubt → partial.
   - **Buyback rights**
   - **Stepdown** — rate decreases after a milestone (production, date, …)
   - **Commodity-specific** — instrument on a specific metal only (e.g. a gold royalty on a Cu-Au-Ag mine) — **NEW**
   - **Sliding scale** — rate changes on conditions (e.g. metal price)
   - **Area of interest** — e.g. "extends a 10 km radius around any claim added to the property" — **NEW**
   - (Streams carry many more features but are less common — note, don't fully model yet.)
   → a features **re-extraction** with this taxonomy. See decision D-3 on the current extra fields.

## C. Big — design first, then build
9. **"Memory" / instrument version-chain** — the flagship ask, and the one that needs thought:
   - A newer report on an instrument already in the DB should **append** new data/context — not replace it,
     not create a separate duplicate.
   - Model as a **chain per instrument**: newest version shown when opened; historical versions hidden but
     available.
   - **Validated entries take priority**, but once a new source is processed the instrument **requires
     validation again.**
   - → This extends our current dedup (dup_key / is_primary / "corroborated by N") into a proper
     source-versioned chain + review lifecycle. It also answers the staleness gap we already flagged.
10. **Multiple instruments per source report** — extraction already returns an array per report; verify it
    holds through the new chain model so distinct instruments on one property stay distinct.
11. **MarketWatch → LODE link** — let news-release-derived entries flow into the DB, flagged **"MarketWatch"**
    (the `ingested_from` column already exists), gated behind validation like everything else.

## D. Decisions to make (some need Matt)
- **D-1 Web search for current status** — Matt would trust status more if Claude could web-search a project's
  current state, but is unsure they want to go there yet. **Parked**; tied to A-1.
- **D-2 Drop `stage` entirely, or keep it point-in-time?** Recommendation: keep the datum but stamp it
  "stage as of <report date>" and remove it as a *filter* — a dated stage isn't wrong, just historical.
- **D-3 The 3 current extra features** (advance payments, production cap/threshold, ROFR) — keep and rename,
  or fold into "streams have more features"? (Matt's list dropped them.)

## Chase Matt for
- The **Tier 2 / Tier 3 country list**.
- Confirm the **competitor list** file (`OR_Competitor_List_2026-08-04.xlsx`).
- Confirm **A-5** (rename verbatim panel → "instrument description").

## Already supported — don't rebuild
- Multi-metal (`commodity` array) · Operators & Holders naming · `ingested_from='marketwatch'` column ·
  multiple royalties per report in extraction · dedup + "corroborated by N reports" (the foundation for C-9).

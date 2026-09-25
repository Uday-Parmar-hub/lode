# MarketWatch to LODE: integration findings (Phase 1 dry-run)

> **HISTORICAL — superseded by [`marketwatch_bridge.md`](marketwatch_bridge.md).**
> This is the read-only investigation that justified building the bridge. It predates the
> implementation and describes an earlier design; several details here are no longer true (the
> extraction is no longer fed `angle`, releases are identified by document rather than story, and
> the Cloud Shell export below is missing the columns the batch path needs). Kept for the reasoning
> and the numbers that made the case. For how the feature actually works, read the other file.

Prepared by Uday, 2026-09-14. Audience: Elijah (interim), Matt (on return). Status: proof-of-value done,
read-only. No production data was changed.

## The question

LODE is fed by technical reports (NI 43-101, S-K 1300, JORC), which are lagging by nature. A royalty
created today via a press release does not appear in a technical report for months or years. MarketWatch
already catches those royalty events in near real time (its "royalty_change" category). So: how many of
the royalty events MarketWatch catches are royalties LODE does not yet have?

## The answer

Over roughly 6 weeks (2026-07-30 to 2026-09-11), MarketWatch flagged 71 royalty-change events. Running
them against LODE's current properties:

- 55 are NEW royalty instruments LODE does not have
- 9 are updates to instruments LODE already tracks
- 7 are framework or portfolio royalties with no single named property

Honest caveat: a few of the 55 are near-duplicates (two press releases about the same deal) or have a
vaguely stated property or holder, so the clean count after a review pass is a bit lower. The order of
magnitude holds: dozens of new royalty instruments every 6 weeks, which is roughly 40-plus a month, or
several hundred a year, that LODE's technical-report feed misses entirely.

## Proof it works (real instruments pulled from press-release prose)

| Property | Rate / Type | Holder |
|---|---|---|
| Berrio Project | 2% NSR | AngloGold Ashanti |
| Barsele Gold Project | 2% NSR | Agnico Eagle Sweden |
| Omagh Gold Project | 3.00% NSR | Galantas |
| Golden Rose claims | 2.0% NSR | Conquest Resources |
| El Domo | US$175.5M stream | (not named) |

These are exactly the shape of a LODE instrument, extracted straight from the news text.

## The key technical finding

90% of MarketWatch's royalty_change stories (64 of 71) have no resolved asset link (sp_asset_id). The
property, rate, and holder live in the prose, not in a structured column, because these are mostly junior
properties MarketWatch does not track in its watchlist. A naive field-to-field join therefore places only
about 10% of events. To use the rest you must read the prose with Claude and extract the instrument.

This is useful two ways. It tells us the integration genuinely needs the Claude re-extraction step (it is
not optional). And the events that are hardest to place are exactly the new-royalty-on-a-junior-property
deals, which are the prime origination signal.

## How it would work

One-way, pull-based, so the two production systems stay decoupled and LODE's reviewable-ledger discipline
is preserved:

1. Pull MarketWatch royalty_change stories since the last sync (one read-only query).
2. Re-extract each source press release into a LODE instrument (property, type, rate, holder) with Claude.
3. Match against existing LODE instruments (property plus holder). MarketWatch's own "claimed_exposure"
   flag routes new vs owner-changed for free, which the memory-chain model already handles.
4. Write candidates to a reviewable ledger (origin = marketwatch, status = pending). Nothing goes live.
5. A human validates, then applies. In the dashboard the entry carries a "from MarketWatch" badge so it
   reads as lower-confidence than a technical-report source.

The hooks already exist on both sides: MarketWatch's royalty_change category and claimed_exposure flag,
and LODE's origin / ingested_from = 'marketwatch' columns plus the memory-chain fields.

## One decision for Matt

A press release often announces a deal that has not closed (a letter of intent, a proposed sale). LODE
stores real instruments. So news-sourced entries need either an "announced, unconfirmed" status or a
closed-only filter. This is a policy call, not a coding one.

## Phasing

- Phase 1 (done): the read-only dry-run above. Proves the value and the mechanism.
- Phase 2: re-extract into a reviewable candidate ledger, matched via the memory chain, human-gated apply.
- Phase 3: a "from MarketWatch" filter and badge in the dashboard, with the news source in the version chain.

## Recommendation

The signal is real and large enough to justify Phase 2. It depends only on the memory-chain foundation,
which has shipped. The main thing to settle first is the announced-vs-closed policy above.

## Reproduce the numbers

Read-only. Nothing writes to production.

```bash
# 1. In Azure Cloud Shell, export the royalty_change stories (one read-only SELECT):
psql "<MarketWatch prm DSN>" -At -c "SELECT json_agg(row_to_json(t)) FROM (
  SELECT DISTINCT ON (s.document_id)
         a.canonical_name AS asset_name, c.canonical_name AS company_name,
         s.core_commodity AS commodity, s.claimed_exposure, s.angle,
         s.published_at::text AS published_at
  FROM stories s
  LEFT JOIN assets a ON a.sp_asset_id=s.sp_asset_id
  LEFT JOIN companies c ON c.sp_company_id=s.sp_company_id
  WHERE s.tier='royalty_change' AND s.is_duplicate=false
  ORDER BY s.document_id, s.published_at DESC) t" > mw_royalty_changes.json

# 2. Locally, the property-level match (no Claude):
python scripts/dryrun_marketwatch_lode.py --mw-file mw_royalty_changes.json

# 3. Locally, the prose re-extraction that gives the real new-vs-tracked number:
python scripts/dryrun_reextract_match.py --mw-file mw_royalty_changes.json
```

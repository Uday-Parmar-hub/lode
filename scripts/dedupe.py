"""Recompute `is_primary` by a canonical dedup key. Non-destructive and re-runnable.

    python scripts/dedupe.py

Same real-world royalty == same (normalized asset | canonical royalty-type family | rate |
normalized holder). This is **precision-first**: distinct holders on one asset stay distinct
rows, so a genuine royalty *stack* (e.g. Gold Bar's 11 separate 1% NSRs held by different
parties) is preserved — only exact re-reports and asset/type/holder *spelling* variants collapse.

Nothing is deleted. `is_primary` is only a display flag (the grid defaults to primary rows), and
the canonical key is stored in `dup_key` so the grouping is auditable and the UI can show
"N reports for this royalty". Within a dup_key the surfaced (primary) row is the newest report,
then source-verified, then highest confidence.

Known residual (needs the semantic pass, not this deterministic one): the *same* royalty split
across a genuine holder change over time (Whistler: MF2 -> Gold Royalty -> Osisko) or an asset
rename (Whistler <-> Whistler Gold-Copper). Those differ on holder/asset text so this pass keeps
them separate on purpose — merging them safely requires entity resolution.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, db  # noqa: E402

# Semantic ledgers (LLM-proposed, human-reviewable). Both optional — without them dedupe.py is the
# pure deterministic pass 1. holder_merges = pass 2 (resolve_holders.py); asset_aliases = pass 2b
# (resolve_assets.py, asset renames applied when building the key).
LEDGER = config.ROOT / "data" / "holder_merges.json"
ASSET_LEDGER = config.ROOT / "data" / "asset_aliases.json"

# accented -> ascii fold (Kandiolé -> kandiole, etc.)
_ACC = "'áàâäãéèêëíìîïóòôöõúùûüçñ','aaaaaeeeeiiiiooooouuuucn'"

# normalized asset: drop parentheticals, fold accents, keep only [a-z0-9]
_NASSET = (
    r"regexp_replace(translate("
    r"regexp_replace(lower(project_name),'\(.*?\)','','g'),"
    f"{_ACC}),'[^a-z0-9]+','','g')"
)

# royalty-type FAMILY — collapses spelling variants ("NPI"/"Net Profit Interest"/"NPI (…)")
# but keeps genuinely different instruments (NSR vs NPI vs stream vs …) distinct, so a
# multi-instrument asset like Casino is not over-merged.
_CTYPE = (
    "case "
    "when lower(coalesce(royalty_type,'')) ~ 'stream' then 'STREAM' "
    "when lower(coalesce(royalty_type,'')) ~ 'nsr|net smelter' then 'NSR' "
    "when lower(coalesce(royalty_type,'')) ~ 'npi|net prof|net proc' then 'NPI' "
    "when lower(coalesce(royalty_type,'')) ~ 'gross|gor|gsr|overrid|gvr' then 'GROSS' "
    "when lower(coalesce(royalty_type,'')) ~ 'advance|amr' then 'AMR' "
    "when lower(coalesce(royalty_type,'')) ~ 'production payment' then 'PRODPMT' "
    "else lower(coalesce(royalty_type,'?')) end"
)

# rate: the parsed % where we have one, else a normalized rate string (catches "US$5/t" re-reports)
_RKEY = r"coalesce(rate_pct::text, regexp_replace(lower(coalesce(rate,'')),'[^a-z0-9.]','','g'))"

# normalized holder: drop parentheticals + legal-form/generic suffixes, fold accents, keep the
# distinctive name. Distinct parties stay distinct; only spelling variants of one party merge.
_NHOLD = (
    r"regexp_replace(regexp_replace(translate("
    r"regexp_replace(lower(coalesce(holder,'')),'\(.*?\)','','g'),"
    f"{_ACC}),"
    r"'\y(inc|incorporated|ltd|limited|llc|l\.l\.c|corp|corporation|company|co|plc|sarl|"
    r"s\.a\.r\.l|sa|s\.a|nl|ag|pty|group|holdings?|resources?|minerals?|mining)\y','','g'),"
    r"'[^a-z0-9]+','','g')"
)

# pass 2b: remap an asset's normalized key to its group's canonical key (via the asset_alias temp table),
# so a renamed asset's royalties share the key with the canonical name's. No ledger -> empty table -> no-op.
_ASSET = f"coalesce((select aa.to_key from asset_alias aa where aa.from_key = {_NASSET}), {_NASSET})"
DUPKEY_SQL = f"({_ASSET}||'|'||{_CTYPE}||'|'||{_RKEY}||'|'||{_NHOLD})"


def load_asset_aliases(cur) -> int:
    """Stage the asset-rename ledger into a temp table used by DUPKEY_SQL. Returns member rows mapped."""
    cur.execute("create temp table asset_alias (from_key text primary key, to_key text) on commit drop")
    if not ASSET_LEDGER.exists():
        return 0
    groups = json.loads(ASSET_LEDGER.read_text(encoding="utf-8"))
    n = 0
    for g in groups:
        ck = g["canonical_key"]
        for k in g["member_keys"]:
            if k == ck:
                continue
            cur.execute("insert into asset_alias(from_key,to_key) values (%s,%s) on conflict do nothing", (k, ck))
            n += 1
    return n


def apply_ledger(cur) -> int:
    """Apply the semantic merges: point every merged row's dup_key at the canonical row's dup_key,
    so the whole lineage shares one canonical group (and one corroboration count). Idempotent."""
    if not LEDGER.exists():
        return 0
    merges = json.loads(LEDGER.read_text(encoding="utf-8"))
    applied = 0
    for m in merges:
        cur.execute("select dup_key from royalties where id=%s", (m["canonical_id"],))
        row = cur.fetchone()
        if not row:
            continue
        kc = row[0]
        for mid in m["member_ids"]:
            if mid == m["canonical_id"]:
                continue
            cur.execute("select dup_key from royalties where id=%s", (mid,))
            r2 = cur.fetchone()
            if not r2 or r2[0] == kc or r2[0] is None:
                continue
            # move the member's whole re-report group under the canonical key
            cur.execute("update royalties set dup_key=%s where dup_key=%s", (kc, r2[0]))
        applied += 1
    return applied


def main() -> None:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(*), count(*) filter (where is_primary) from royalties")
        total, before = cur.fetchone()

        cur.execute("alter table royalties add column if not exists dup_key text")
        # bookkeeping updates must not bump updated_at ("Date Modified") — pause the touch trigger
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            asset_merges = load_asset_aliases(cur)  # pass 2b: stage asset renames (no-op if absent)
            cur.execute(f"update royalties set dup_key = {DUPKEY_SQL}")
            cur.execute("create index if not exists idx_roy_dupkey on royalties (dup_key)")
            merges = apply_ledger(cur)  # semantic pass 2 (no-op if the ledger is absent)
            # surface the newest / most-trustworthy row per dup_key; retain the rest (is_primary=false)
            cur.execute(
                """
                with ranked as (
                  select id, row_number() over (
                    partition by dup_key
                    order by source_date desc nulls last, quote_verified desc,
                             extract_confidence desc nulls last, id desc
                  ) as rn
                  from royalties
                )
                update royalties r set is_primary = (ranked.rn = 1)
                from ranked where ranked.id = r.id
                """
            )
        finally:
            cur.execute("alter table royalties enable trigger trg_roy_touch")

        cur.execute("select count(*) filter (where is_primary) from royalties")
        after = cur.fetchone()[0]
        conn.commit()

    parts = []
    if asset_merges:
        parts.append(f"{asset_merges} asset-rename aliases")
    if merges:
        parts.append(f"{merges} holder-merges")
    note = f" (incl. {', '.join(parts)})" if parts else " (deterministic only; no ledgers)"
    print(f"rows: {total}   primary: {before} -> {after}   (collapsed {before - after} duplicates){note}")


if __name__ == "__main__":
    main()

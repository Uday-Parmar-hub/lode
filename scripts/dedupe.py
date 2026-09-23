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
# The dup-key definition, the primary ordering and the asset-alias staging now live in
# techreport.chain, so the button and the batch loader use the same ones rather than copies.
from techreport.chain import (  # noqa: E402
    ASSET_LEDGER, DUPKEY_SQL, load_asset_aliases, set_primary,
)

# Semantic ledgers (LLM-proposed, human-reviewable). Both optional — without them dedupe.py is the
# pure deterministic pass 1. holder_merges = pass 2 (resolve_holders.py); asset_aliases = pass 2b
# (resolve_assets.py, asset renames applied when building the key).
LEDGER = config.ROOT / "data" / "holder_merges.json"
# audit-confirmed merges (Fable-5 duplicate audit -> apply_audit_fixes.py), same {canonical_id,member_ids}
# format as holder_merges but a SEPARATE file so re-running resolve_holders.py can't clobber them.
AUDIT_LEDGER = config.ROOT / "data" / "audit_merges_ledger.json"



def apply_ledger(cur) -> int:
    """Apply the semantic merges: point every merged row's dup_key at the canonical row's dup_key,
    so the whole lineage shares one canonical group (and one corroboration count). Idempotent.
    Reads BOTH the holder-resolution ledger (pass 2) and the audit-confirmed-merges ledger."""
    applied = 0
    for path in (LEDGER, AUDIT_LEDGER):
        if not path.exists():
            continue
        for m in json.loads(path.read_text(encoding="utf-8")):
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
            set_primary(cur, "dup_key is not null", ())
            # keep instrument_id consistent with the dup_key groups: one stable id per group, REUSING an
            # existing id in the group where present (so confirmed merges / prior ids survive a re-run),
            # minting a fresh one only for groups that have none. Makes instrument_id a durable output.
            cur.execute(
                """
                with grp as (
                  select dup_key,
                         coalesce(
                           max(instrument_id) filter (where instrument_id is not null),
                           'inst_'||substr(md5(dup_key||clock_timestamp()::text||random()::text),1,20)
                         ) as iid
                  from royalties where dup_key is not null group by dup_key
                )
                update royalties r set instrument_id = grp.iid from grp where r.dup_key = grp.dup_key
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

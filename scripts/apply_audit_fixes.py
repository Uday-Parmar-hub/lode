"""Apply confirmed audit MERGES (from the Fable-5 duplicate audit) — human-gated, non-destructive.

    python scripts/apply_audit_fixes.py            # DRY RUN — show the plan + projected counts, commit nothing
    python scripts/apply_audit_fixes.py --apply    # commit the merges

Reads data/audit_merges.json (groups of instrument_ids the audit + a human confirmed are the SAME
royalty). For each non-held group it unifies the instruments into one chain: every member's rows get
the canonical instrument_id + dup_key (canonical = the instrument whose primary is the NEWEST source),
is_primary is recomputed (newest source = shown), and the resulting primary is flagged
needs_revalidation so an analyst re-confirms the merged record.

Non-destructive: nothing is deleted (rows are re-pointed, not removed) and it's fully reversible by a
re-run of the dedup pipeline. Groups with "hold": true are skipped (left for manual review). The whole
run is one transaction, ROLLED BACK unless --apply, so the dry run reports the true projected counts.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, db  # noqa: E402

LEDGER = config.ROOT / "data" / "audit_merges.json"


def main(apply: bool) -> None:
    groups = json.loads(LEDGER.read_text(encoding="utf-8"))
    todo = [g for g in groups if not g.get("hold")]
    held = [g for g in groups if g.get("hold")]
    print(f"merge groups: {len(todo)} to apply, {len(held)} held\n")

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(distinct instrument_id) from royalties")
        before = cur.fetchone()[0]
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            for g in todo:
                ids = g["instrument_ids"]
                # canonical = instrument whose primary row has the newest source_date
                cur.execute(
                    """select instrument_id, dup_key from royalties
                        where is_primary and instrument_id = any(%s)
                        order by source_date desc nulls last, created_at desc, id desc limit 1""",
                    (ids,))
                canon_iid, canon_dup = cur.fetchone()
                others = [i for i in ids if i != canon_iid]
                cur.execute(
                    "update royalties set instrument_id=%s, dup_key=%s where instrument_id = any(%s)",
                    (canon_iid, canon_dup, others))
                moved = cur.rowcount
                # recompute the sole primary of the unified chain = newest source row
                cur.execute("update royalties set is_primary=false where instrument_id=%s", (canon_iid,))
                cur.execute(
                    """update royalties set is_primary=true where id=(
                         select id from royalties where instrument_id=%s
                         order by source_date desc nulls last, created_at desc, id desc limit 1)""",
                    (canon_iid,))
                cur.execute(
                    "update royalties set needs_revalidation=true where instrument_id=%s and is_primary",
                    (canon_iid,))
                cur.execute(
                    "select project_name, holder, source_label from royalties where instrument_id=%s and is_primary",
                    (canon_iid,))
                pn, holder, src = cur.fetchone()
                print(f"  {g['asset']}: {len(ids)} → 1  | current: {holder!r} ({src}) | +{moved} rows re-pointed")

            cur.execute("select count(distinct instrument_id) from royalties")
            after = cur.fetchone()[0]
            cur.execute("select count(*) filter (where is_primary), count(*) from royalties")
            prim, tot = cur.fetchone()
        finally:
            cur.execute("alter table royalties enable trigger trg_roy_touch")

        print(f"\ninstruments: {before} → {after}  (−{before - after}) | primary={prim} total={tot} (total must stay 1149)")
        if apply:
            conn.commit(); print("COMMITTED.")
        else:
            conn.rollback(); print("DRY RUN — rolled back. Re-run with --apply to commit.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)

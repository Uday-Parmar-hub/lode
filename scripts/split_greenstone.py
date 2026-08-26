"""One-off fix: Greenstone Gold Mine — un-bundle two royalties the extraction fused into one row.

    python scripts/split_greenstone.py            # DRY RUN
    python scripts/split_greenstone.py --apply

The extraction recorded "Placer Dome 2.25% NSR / Key Lake 2% NSR" as a SINGLE instrument (both rows
bundle the two). This corrects the existing instrument to the Placer Dome 2.25% NSR, and creates a
NEW instrument for the Key Lake 2% NSR (copying the asset facts). Both are flagged needs_revalidation
so an analyst confirms. Non-destructive (existing rows corrected in place + one new row added), one txn,
rolled back unless --apply. (This is the manual-split pattern that increment 5's "Add royalty" will make
a UI action; done here as a targeted fix.)
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import db  # noqa: E402

IID = "inst_61a94a6dbad011af8912"   # the bundled Greenstone instrument
COPY_FROM = 323                      # its primary row (newest report) — source of the asset facts

# columns copied verbatim from the source row for the new Key Lake instrument
_COPY = ("sp_id, project_name, operator, commodity, jurisdiction, stage, est_startup, royalty_available, "
         "extract_confidence, royalty_created, info_available, regime, source_label, source_url, source_date, "
         "source_quote, quote_verified, country, state_province, continent, jurisdiction_tier, ingested_from")


def main(apply: bool) -> None:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(distinct instrument_id) from royalties")
        before = cur.fetchone()[0]
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            # 1) correct the existing instrument -> Placer Dome 2.25% NSR only (values as params so
            #    the '%' in the rate literals isn't parsed as a placeholder)
            cur.execute(
                """update royalties set holder=%s, rate=%s, rate_pct=%s, royalty_type=%s,
                   origin='claude_human_edited', needs_revalidation=true
                 where instrument_id=%s""",
                ("Placer Dome Inc.", "2.25% NSR", 2.25, "NSR", IID))
            fixed = cur.rowcount
            # 2) create a NEW instrument for the Key Lake 2% NSR
            cur.execute(
                f"""insert into royalties (
                     {_COPY}, holder, holder_note, rate, rate_pct, royalty_type,
                     instrument_id, dup_key, source_docid, origin, status, is_primary,
                     needs_revalidation, created_at, updated_at)
                   select {_COPY}, %s, %s, %s, %s::numeric, %s,
                     'inst_'||substr(md5('greenstone-keylake'||clock_timestamp()::text||random()::text),1,20),
                     %s, source_docid||%s, 'claude_human_edited', 'pending', true, true, now(), now()
                   from royalties where id=%s
                   returning id, instrument_id""",
                ("Key Lake Exploration", "split from a bundled Placer Dome / Key Lake extraction",
                 "2% NSR", 2, "NSR", "greenstonegoldmine|NSR|2|keylakeexploration",
                 "#split-keylake", COPY_FROM))
            new_id, new_iid = cur.fetchone()
        finally:
            cur.execute("alter table royalties enable trigger trg_roy_touch")
        cur.execute("select count(distinct instrument_id) from royalties")
        after = cur.fetchone()[0]
        print(f"Placer Dome instrument: {fixed} rows corrected to 2.25% NSR (needs re-validation)")
        print(f"Key Lake instrument: new row id={new_id} instrument={new_iid} (2% NSR, needs re-validation)")
        print(f"instruments: {before} → {after}  (+{after - before})")
        if apply:
            conn.commit(); print("COMMITTED.")
        else:
            conn.rollback(); print("DRY RUN — rolled back. Re-run with --apply to commit.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)

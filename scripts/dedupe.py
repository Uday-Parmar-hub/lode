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

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import db  # noqa: E402

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

DUPKEY_SQL = f"({_NASSET}||'|'||{_CTYPE}||'|'||{_RKEY}||'|'||{_NHOLD})"


def main() -> None:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(*), count(*) filter (where is_primary) from royalties")
        total, before = cur.fetchone()

        cur.execute("alter table royalties add column if not exists dup_key text")
        # bookkeeping updates must not bump updated_at ("Date Modified") — pause the touch trigger
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            cur.execute(f"update royalties set dup_key = {DUPKEY_SQL}")
            cur.execute("create index if not exists idx_roy_dupkey on royalties (dup_key)")
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

    print(f"rows: {total}   primary: {before} -> {after}   (collapsed {before - after} duplicate re-reports)")


if __name__ == "__main__":
    main()

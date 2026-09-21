"""Shared identity + memory-chain logic for the royalties table.

ONE definition of "the same real-world royalty" and ONE definition of which version of it is the
surfaced (primary) one, used by every writer:

  * scripts/dedupe.py        — the canonical table-wide pass (also applies the semantic ledgers)
  * scripts/add_one_to_lode.py — the MarketWatch "Add to LODE" button, one release at a time
  * scripts/load_royalties.py  — the batch ledger loader

These used to live in dedupe.py alone, so the other two writers either re-implemented them or left
their rows unlinked — and an unlinked row (NULL instrument_id) breaks the dashboard's edit path,
whose demote runs `WHERE instrument_id = <id>` and matches nothing, leaving two current versions of
one royalty.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from techreport import config  # noqa: E402

# asset-rename ledger (pass 2b), staged into a temp table that DUPKEY_SQL reads. Optional.
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

# Which version of an instrument is the surfaced one. Validated first: a record the desk has signed
# off is NOT displaced by an unreviewed one just because the unreviewed one is newer — a press release
# always has the newest source_date, so without this term one click could replace a validated
# technical-report row in every default view. The new source still raises needs_revalidation, which is
# how it gets looked at. (No rows are 'validated' yet, so this term is currently a no-op.)
PRIMARY_ORDER_SQL = """
    (status = 'validated') desc,
    source_date desc nulls last,
    quote_verified desc,
    extract_confidence desc nulls last,
    id desc
"""


def ensure_asset_alias(cur) -> None:
    """Create/populate the asset_alias temp table once per transaction.

    load_asset_aliases uses `create temp table` without IF NOT EXISTS, and link() may run more than
    once in a transaction (two releases, or a test), so guard on the table already existing.
    """
    cur.execute("select to_regclass('asset_alias') is not null")
    if not cur.fetchone()[0]:
        load_asset_aliases(cur)


def link(cur, where_sql: str, params: tuple) -> dict:
    """Assign dup_key / instrument_id / is_primary to the rows matched by `where_sql`.

    `where_sql` is a literal fragment from our own code (e.g. "source_docid = %s"), `params` are bound.
    Returns {instruments, joined_existing, flagged_revalidation}.

    Scoped to the groups the new rows touch, using the same key and the same ordering as the canonical
    dedupe.py pass, so the two can never disagree. A full dedupe.py run remains the authority — it also
    applies the holder/asset semantic ledgers, which this does not.

    Every UPDATE is written to be a no-op when the value would not change. That matters because the
    BEFORE UPDATE trigger trg_roy_touch rewrites updated_at ("Date Modified"): dedupe.py suppresses it
    by disabling the trigger around its bookkeeping, which is not safe here (DISABLE TRIGGER is
    table-wide, not session-scoped, so a crash mid-click would leave it off for everyone). Not writing
    unchanged rows gets the same result without touching the trigger.
    """
    ensure_asset_alias(cur)

    # DUPKEY_SQL carries no placeholders, so `params` binds only where_sql's. The `is distinct from`
    # guard keeps this a no-op for rows whose key is unchanged, so trg_roy_touch does not re-date them.
    cur.execute(f"update royalties set dup_key = {DUPKEY_SQL} "
                f"where ({where_sql}) and dup_key is distinct from {DUPKEY_SQL}", params)
    cur.execute(f"select distinct dup_key from royalties where ({where_sql}) and dup_key is not null",
                params)
    keys = [r[0] for r in cur.fetchall()]
    if not keys:
        return {"instruments": 0, "joined_existing": 0, "flagged_revalidation": 0}

    # groups that already existed => this source corroborates a royalty the corpus already held
    cur.execute(f"""select count(*) from (
                      select dup_key from royalties
                       where dup_key = any(%s) and not ({where_sql})
                       group by dup_key) g""", (keys,) + params)
    joined = cur.fetchone()[0]

    # reuse the group's existing instrument id where there is one; mint only for a new instrument
    cur.execute("""
        with grp as (
          select dup_key,
                 coalesce(max(instrument_id) filter (where instrument_id is not null),
                          'inst_'||substr(md5(dup_key||clock_timestamp()::text||random()::text),1,20)) as iid
            from royalties where dup_key = any(%s) group by dup_key
        )
        update royalties r set instrument_id = grp.iid
          from grp where r.dup_key = grp.dup_key and r.instrument_id is null
    """, (keys,))

    cur.execute(f"""
        with ranked as (
          select id, row_number() over (partition by dup_key order by {PRIMARY_ORDER_SQL}) as rn
            from royalties where dup_key = any(%s)
        )
        update royalties r set is_primary = (ranked.rn = 1)
          from ranked where ranked.id = r.id and r.is_primary is distinct from (ranked.rn = 1)
    """, (keys,))

    # a new source on an instrument the desk already validated goes back for re-review (migration 003)
    cur.execute("""
        update royalties r set needs_revalidation = true
         where r.dup_key = any(%s) and r.is_primary and not r.needs_revalidation
           and exists (select 1 from royalties v
                        where v.dup_key = r.dup_key and v.status = 'validated')
    """, (keys,))
    flagged = cur.rowcount

    return {"instruments": len(keys), "joined_existing": joined, "flagged_revalidation": flagged}

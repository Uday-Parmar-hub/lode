"""Semantic dedup (pass 2b): resolve ASSET renames / alternate names.

    python scripts/resolve_assets.py                 # all candidate pairs
    python scripts/resolve_assets.py --only azules --dry

Pass 2 resolves holders *within* an asset name, so it can't merge the same royalty when it appears
under two different asset names — a rename ("Whistler Project" -> "Whistler Gold-Copper Project"), an
alternate name ("Almaden Gold Property" = "Nutmeg Mountain"), or a qualifier drift ("Los Azules" vs
"Los Azules Copper Project"). This pass blocks candidate asset pairs (a shared royalty fingerprint
type+rate+holder across two names, or same operator + similar name), then asks claude-opus-5 whether
they are the SAME physical asset. Precision-favoring — different deposits/zones/separate properties of
one operator are NOT the same asset; when unsure, keep separate.

Confirmed pairs are unioned into groups and written to data/asset_aliases.json (gitignored, like all of
data/). scripts/dedupe.py maps every member's normalized-asset key to the group's canonical key when it
builds dup_key, so the two names' matching royalties unify. The surfaced row's DISPLAY name still comes
from the newest report — the alias only unifies the grouping key. This script only writes the ledger.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import anthropic  # noqa: E402

from techreport import config, db  # noqa: E402

LEDGER = config.ROOT / "data" / "asset_aliases.json"
MODEL = "claude-opus-5"

_ACC = "'áàâäãéèêëíìîïóòôöõúùûüçñ','aaaaaeeeeiiiiooooouuuucn'"
_NA = (rf"regexp_replace(translate(regexp_replace(lower(project_name),'\(.*?\)','','g'),{_ACC}),"
       r"'[^a-z0-9]+','','g')")
_CT = ("case when lower(coalesce(royalty_type,''))~'stream' then 'STREAM' "
       "when lower(coalesce(royalty_type,''))~'nsr|net smelter' then 'NSR' "
       "when lower(coalesce(royalty_type,''))~'npi|net prof|net proc' then 'NPI' "
       "when lower(coalesce(royalty_type,''))~'gross|gor|gsr|overrid|gvr' then 'GROSS' "
       "when lower(coalesce(royalty_type,''))~'advance|amr' then 'AMR' "
       "when lower(coalesce(royalty_type,''))~'production payment' then 'PRODPMT' "
       "else lower(coalesce(royalty_type,'?')) end")
_RK = r"coalesce(rate_pct::text,regexp_replace(lower(coalesce(rate,'')),'[^a-z0-9.]','','g'))"
_NH = (rf"regexp_replace(regexp_replace(translate(regexp_replace(lower(coalesce(holder,'')),'\(.*?\)','','g'),{_ACC}),"
       r"'\y(inc|incorporated|ltd|limited|llc|l\.l\.c|corp|corporation|company|co|plc|sarl|"
       r"s\.a\.r\.l|sa|s\.a|nl|ag|pty|group|holdings?|resources?|minerals?|mining)\y','','g'),'[^a-z0-9]+','','g')")

SYSTEM = """You decide whether two entries in a mining database are the SAME physical asset.

You are given two asset entries (name, operator, jurisdiction, commodities) and any royalties they share.
Say SAME only if they are the same physical project/property — a rename, an alternate or historical name,
an abbreviation, or the same project with/without a descriptive qualifier or development phase
("X" vs "X Copper Project" vs "X Mine" vs "X Complex" vs "X Phase II").

Say NOT SAME if they are genuinely different assets — separate deposits, zones, or properties, even under
one operator or in one district, and even if they share a royalty holder (a royalty company like
Franco-Nevada or Osisko holds royalties across many different assets, so a shared holder is NOT by itself
evidence of the same asset). When you are unsure, answer NOT SAME — wrongly merging two real assets is
worse than leaving a duplicate."""

TOOL = {
    "name": "decide",
    "description": "Whether the two entries are the same physical asset.",
    "input_schema": {
        "type": "object",
        "properties": {
            "same_asset": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["same_asset", "reason"],
    },
}


def fetch_candidates():
    """Return (pairs, ctx). pairs = list of (keyA, keyB); ctx[key] = {name, op, jur, comm, royalties[]}."""
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("set pg_trgm.similarity_threshold=0.3")
        # candidate asset-key pairs: shared royalty fingerprint + similar name, OR same operator + very similar name
        cur.execute(f"""
            with p as (select {_NA} a, {_CT} t, {_RK} r, {_NH} h, operator op from royalties where is_primary)
            select distinct p1.a, p2.a
            from p p1 join p p2 on p1.a < p2.a
            where (p1.t=p2.t and p1.r=p2.r and p1.h=p2.h and p1.h<>'' and similarity(p1.a,p2.a) > 0.3)
               or (p1.op is not distinct from p2.op and p1.op is not null and similarity(p1.a,p2.a) > 0.55)
        """)
        pairs = [(a, b) for a, b in cur.fetchall()]

        keys = sorted({k for pair in pairs for k in pair})
        ctx: dict[str, dict] = {}
        if keys:
            cur.execute(f"""
                select {_NA} a, min(project_name) name, min(operator) op, min(jurisdiction) jur,
                       (array_agg(distinct x))[1:6] comms,
                       (array_agg(distinct (coalesce(royalty_type,'?')||' '||coalesce(rate,'?')||' / '
                            ||coalesce(holder,'?'))))[1:6] royalties
                from royalties, unnest(coalesce(commodity,'{{}}')) x
                where is_primary and {_NA} = any(%s)
                group by 1
            """, (keys,))
            for a, name, op, jur, comms, roys in cur.fetchall():
                ctx[a] = {"name": name, "op": op, "jur": jur, "comm": comms, "royalties": roys}
    return pairs, ctx


def decide_pair(a: str, b: str, ctx: dict) -> tuple[str, str, bool, str]:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    payload = {"entry_1": ctx.get(a, {"name": a}), "entry_2": ctx.get(b, {"name": b})}
    msg = client.messages.create(
        model=MODEL, max_tokens=600, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "decide"},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    block = next(bl for bl in msg.content if bl.type == "tool_use")
    d = dict(block.input)
    return a, b, bool(d.get("same_asset")), d.get("reason", "")


class UF:
    def __init__(self): self.p: dict[str, str] = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="filter candidate pairs by asset-name substring")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    pairs, ctx = fetch_candidates()
    if args.only:
        o = args.only.lower()
        pairs = [(a, b) for a, b in pairs
                 if o in ctx.get(a, {}).get("name", "").lower() or o in ctx.get(b, {}).get("name", "").lower()]
    print(f"{len(pairs)} candidate asset pairs -> confirming on {MODEL}\n")

    uf = UF()
    confirmed = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(decide_pair, a, b, ctx) for a, b in pairs]
        for fut in as_completed(futs):
            try:
                a, b, same, reason = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! ERROR {type(e).__name__}: {str(e)[:80]}")
                continue
            na, nb = ctx.get(a, {}).get("name", a), ctx.get(b, {}).get("name", b)
            mark = "✓ same" if same else "·  distinct"
            print(f"  {mark:11} {na[:30]:32} <=> {nb[:30]:32} — {reason[:60]}")
            if same:
                uf.union(a, b); confirmed.append((a, b, reason))

    # build groups from confirmed unions; canonical = member with the most rows (dominant name)
    groups: dict[str, list[str]] = {}
    for k in {x for pair in confirmed for x in pair[:2]}:
        groups.setdefault(uf.find(k), []).append(k)
    reason_by_root = {}
    for a, b, reason in confirmed:
        reason_by_root.setdefault(uf.find(a), reason)

    with db.connect() as conn:
        cur = conn.cursor()
        rowcount = {}
        for keys in groups.values():
            for k in keys:
                cur.execute(f"select count(*) from royalties where {_NA} = %s", (k,))
                rowcount[k] = cur.fetchone()[0]

    ledger = []
    for root, keys in groups.items():
        canonical = max(keys, key=lambda k: rowcount.get(k, 0))
        ledger.append({
            "canonical_key": canonical,
            "canonical_name": ctx.get(canonical, {}).get("name", canonical),
            "member_keys": sorted(keys),
            "names": [ctx.get(k, {}).get("name", k) for k in sorted(keys)],
            "reason": reason_by_root.get(root, ""),
        })
    collapsed = sum(len(g["member_keys"]) - 1 for g in ledger)
    print(f"\n{len(pairs)} pairs -> {len(ledger)} asset groups merging {collapsed} name-variants.")
    if args.dry:
        print("(dry run — ledger not written)")
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {LEDGER}  — review it, then run scripts/dedupe.py to apply.")


if __name__ == "__main__":
    main()

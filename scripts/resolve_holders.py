"""Semantic dedup (pass 2): resolve holder identity within same-royalty clusters.

    python scripts/resolve_holders.py                # all ambiguous clusters
    python scripts/resolve_holders.py --only whistler --dry   # preview one asset

Pass 1 (scripts/dedupe.py) collapses exact re-reports + spelling variants but keeps DISTINCT holders
apart on purpose. That leaves two safe-but-noisy residuals it can't resolve deterministically:
  • holder DRIFT — one royalty whose owner changed over time (Whistler 2.75% NSR: MF2 -> Gold Royalty -> Osisko)
  • same entity under different names — parent/subsidiary/abbrev (Salares 2% NSR: Franco-Nevada / "a subsidiary
    of Franco-Nevada" / "SLM Rio Baker")
...while a genuine royalty STACK (Gold Bar: 11 unrelated parties each holding a separate 1% NSR) must stay split.

Telling these apart is judgment, so Claude does it — cluster by cluster, precision-favoring (when unsure, keep
DISTINCT). Output is a *proposal ledger* at data/holder_merges.json (same philosophy as data/manual_overrides.json):
a human can review/edit it, and scripts/dedupe.py applies it (rewrites dup_key so merged rows share one canonical
group + corroboration count). Nothing here touches the DB — it only writes the ledger.
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

LEDGER = config.ROOT / "data" / "holder_merges.json"
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

SYSTEM = """You resolve holder identity for a mining-royalty origination database.

You are given ONE cluster of rows that all describe a royalty on the SAME asset, of the SAME type, at the
SAME rate, but recorded with DIFFERENT holder (counterparty) names across technical reports filed in
different years. Decide which rows are the SAME underlying royalty and which are genuinely DISTINCT
royalties (a "royalty stack" — several separate royalties that happen to share a rate).

Group rows as the SAME royalty when the holders are:
- the same entity under name variants, abbreviations, or legal-suffix/ticker differences;
- a parent and its subsidiary / affiliate / holdco (e.g. "Franco-Nevada", "a subsidiary of Franco-Nevada",
  and a named subsidiary such as "SLM Rio Baker" are one);
- the SAME royalty conveyed / assigned / transferred between parties OVER TIME (holder drift). The
  holder_note usually states the lineage ("originally MF2 LLC; conveyed to Gold Royalty; now Osisko").
  Treat the whole lineage as one royalty; the current holder is the one in the newest report.
- a row with a null/blank holder that clearly refers to the same royalty as a named row in the cluster.

Keep rows as DISTINCT royalties when the holders are genuinely different, unrelated parties each holding a
separate royalty on the property. Different named individuals or unrelated companies with no stated
relationship are DISTINCT. When you are UNSURE whether two names are the same party (or a real transfer of
one royalty) versus two separate royalties, keep them DISTINCT — never merge on a guess. Losing a real,
separate royalty is worse than leaving a duplicate.

Every input row id must appear in EXACTLY ONE group. A distinct royalty is its own single-member group.
For each group, set canonical_id to the row reflecting the CURRENT holder (usually the newest year), and
give a one-line reason."""

TOOL = {
    "name": "resolve",
    "description": "Partition the cluster's rows into groups; each group is one underlying royalty.",
    "input_schema": {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "member_ids": {"type": "array", "items": {"type": "integer"}},
                        "canonical_id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["member_ids", "canonical_id", "reason"],
                },
            }
        },
        "required": ["groups"],
    },
}


def fetch_clusters(only: str | None):
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            with p as (
              select id, project_name, operator, holder, holder_note, royalty_type, rate,
                     extract(year from source_date)::int as yr, source_label,
                     {_NA} a, {_CT} t, {_RK} r
              from royalties where is_primary
            ),
            amb as (select a,t,r from p group by a,t,r having count(*)>1)
            select p.a,p.t,p.r, p.id, p.project_name, p.operator, p.holder, p.holder_note,
                   p.royalty_type, p.rate, p.yr, p.source_label
            from p join amb using (a,t,r)
            order by p.a,p.t,p.r, p.yr desc nulls last
        """)
        rows = cur.fetchall()
    clusters: dict[tuple, dict] = {}
    for a, t, r, rid, pn, op, hold, note, rtype, rate, yr, src in rows:
        key = (a, t, r)
        c = clusters.setdefault(key, {"asset": pn, "rows": []})
        c["rows"].append({"id": rid, "holder": hold, "holder_note": note,
                          "year": yr, "type": rtype, "rate": rate, "source": src, "operator": op})
    out = list(clusters.values())
    if only:
        o = only.lower()
        out = [c for c in out if o in (c["asset"] or "").lower()]
    return out


def resolve_one(cluster: dict) -> list[dict]:
    """Return merge groups (len>1) for one cluster, or [] on distinct/invalid."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    ids = {row["id"] for row in cluster["rows"]}
    payload = {"asset": cluster["asset"],
               "rows": [{k: row[k] for k in ("id", "holder", "holder_note", "year", "source", "operator")}
                        for row in cluster["rows"]]}
    msg = client.messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "resolve"},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    block = next(b for b in msg.content if b.type == "tool_use")
    groups = dict(block.input).get("groups", [])
    # validate: exact partition of the cluster's ids
    seen: list[int] = []
    for g in groups:
        seen += list(g.get("member_ids", []))
    if sorted(seen) != sorted(ids):
        return [{"_invalid": True, "asset": cluster["asset"], "got": seen, "want": sorted(ids)}]
    merges = []
    for g in groups:
        m = list(g["member_ids"])
        if len(m) > 1:
            cid = g["canonical_id"] if g["canonical_id"] in m else m[0]
            merges.append({"asset": cluster["asset"], "canonical_id": cid,
                           "member_ids": m, "reason": g.get("reason", "")})
    return merges


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="filter clusters by asset-name substring")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry", action="store_true", help="print, don't write the ledger")
    args = ap.parse_args()

    clusters = fetch_clusters(args.only)
    print(f"{len(clusters)} ambiguous clusters "
          f"({sum(len(c['rows']) for c in clusters)} primary rows) -> resolving on {MODEL}\n")

    all_merges: list[dict] = []
    invalid = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(resolve_one, c): c for c in clusters}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 - report + continue, never abort the whole run
                print(f"  ! {c['asset'][:30]:32} ERROR {type(e).__name__}: {str(e)[:80]}")
                invalid += 1
                continue
            done += 1
            if res and res[0].get("_invalid"):
                invalid += 1
                print(f"  ? {c['asset'][:30]:32} invalid partition, skipped")
                continue
            for m in res:
                all_merges.append(m)
                print(f"  ✓ {m['asset'][:30]:32} merge {len(m['member_ids'])} rows  — {m['reason'][:70]}")
    collapsed = sum(len(m["member_ids"]) - 1 for m in all_merges)
    print(f"\n{done} clusters resolved, {invalid} skipped/errored.  "
          f"{len(all_merges)} merges covering {collapsed} collapsible rows.")
    if args.dry:
        print("(dry run — ledger not written)")
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(all_merges, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {LEDGER}  — review it, then run scripts/dedupe.py to apply.")


if __name__ == "__main__":
    main()

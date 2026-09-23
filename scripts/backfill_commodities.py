"""Re-derive the `commodity` array for rows the old parser got wrong. DRY RUN unless --apply.

    python scripts/backfill_commodities.py                       # report only, writes nothing
    python scripts/backfill_commodities.py --apply               # snapshot, then update
    python scripts/backfill_commodities.py --emit-sql out.sql    # UPDATEs to run in Cloud Shell

Why this exists: the previous commodity parser matched only single bare words against a 20-entry
map and then fell back to "1-4 chars starting uppercase". It dropped 42% of EDGAR's commodity
strings and 15% of the pilot's, leaving rows with an empty array — which the board's commodity
filter excludes entirely, including its catch-all "Other" chip (an empty array satisfies no chip).
The same fallback also invented commodities: Ag), MOP), Ru) and VHM) are stored as if they were
symbols. techreport.commodity now parses these correctly; this re-runs it over the rows already
written, taking the raw string from the ledger the row was loaded from.

DELIBERATELY NARROW. A row is only touched when its stored array is empty or contains a token the
parser would never produce. Rows whose array is merely different (for instance a PGM that was
stored as PGE and would still be PGE) are left alone, so this cannot churn good data.

Rows with no ledger entry cannot be re-derived at all — the MarketWatch bridge parsed the string
and discarded it. Those are reported and skipped; the bridge stores the array correctly now, so it
is a one-off residue.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, db  # noqa: E402
from techreport.commodity import _SYMBOL_ALIAS, NAME2SYM, SYMBOLS, commodities  # noqa: E402

LEDGERS = {
    "edgar": config.ROOT / "data" / "edgar_royalties.json",
    "pilot": config.ROOT / "data" / "royalty_pilot.json",
    "marketwatch": config.ROOT / "data" / "marketwatch_royalties.json",
}
# Anything the current parser can emit. A stored token outside this set is either junk from the old
# fallback (Ag), MOP), VHM)) or a superseded spelling — the aliased forms are excluded deliberately so
# a legacy 'PGMs' is re-derived to 'PGE', which is the board's actual filter chip.
VALID = (set(NAME2SYM.values()) | SYMBOLS) - set(_SYMBOL_ALIAS)


def raw_by_docid() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, path in LEDGERS.items():
        if not path.exists():
            print(f"  (no {name} ledger at {path.name} — those rows cannot be re-derived)")
            continue
        for rec in json.loads(path.read_text(encoding="utf-8")):
            if rec.get("docid") and rec.get("commodity"):
                out[rec["docid"]] = rec["commodity"]
    return out


def sql_array(vals: list[str]) -> str:
    inner = ",".join("'" + v.replace("'", "''") + "'" for v in vals)
    return f"ARRAY[{inner}]::text[]" if vals else "'{}'::text[]"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (snapshots first)")
    ap.add_argument("--emit-sql", metavar="PATH", help="write UPDATE statements instead of connecting")
    ap.add_argument("--limit-samples", type=int, default=10)
    args = ap.parse_args()

    raw = raw_by_docid()
    print(f"ledger commodity strings for {len(raw)} source documents\n")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select id, source_docid, commodity, ingested_from from royalties order by id")
            rows = cur.fetchall()

    planned: list[tuple[int, list[str], list[str], str]] = []   # id, before, after, reason
    stat: collections.Counter = collections.Counter()
    for rid, docid, stored, src in rows:
        stored = list(stored or [])
        junk = [t for t in stored if t not in VALID]
        empty = not stored
        if not (empty or junk):
            stat["left alone (already valid)"] += 1
            continue
        s = raw.get(docid)
        if s is None:
            stat[f"cannot re-derive — no ledger entry [{src}]"] += 1
            continue
        want = commodities(s)
        if want == stored:
            stat["left alone (parser agrees)"] += 1
            continue
        reason = "empty" if empty else f"junk: {','.join(junk)}"
        planned.append((rid, stored, want, reason))
        stat[f"WOULD CHANGE ({'fill empty' if empty else 'drop junk'}) [{src}]"] += 1

    for k, v in sorted(stat.items()):
        print(f"   {v:>5}  {k}")
    print(f"\n   {len(planned):>5}  rows to update")

    if planned:
        print("\nsamples:")
        for rid, before, after, reason in planned[: args.limit_samples]:
            print(f"   id {rid:<6} {str(before):<22} -> {str(after):<34} ({reason})")
        still = [p for p in planned if not p[2]]
        if still:
            print(f"\n   NOTE: {len(still)} would still end up empty (unparseable even now)")

    if args.emit_sql:
        # Keyed on source_docid + the exact value being replaced, NOT on id: this file is meant to be
        # run against a different database (prod, via Cloud Shell) whose id sequence need not match,
        # and the commodity string is a property of the source document, so every royalty from one
        # document shares it. The value guard makes each statement a no-op if that database already
        # holds something else, so the file is safe to re-run and cannot overwrite better data.
        by_doc: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = collections.Counter()
        docid_of = {rid: d for rid, d, _c, _s in rows}
        for rid, before, after, _reason in planned:
            by_doc[(docid_of[rid], tuple(before), tuple(after))] += 1
        out = pathlib.Path(args.emit_sql)
        with out.open("w", encoding="utf-8") as f:
            f.write("-- commodity backfill, generated by scripts/backfill_commodities.py\n")
            f.write("-- Re-derives `commodity` for rows the old parser left empty or filled with junk.\n")
            f.write("-- Keyed on source_docid; each statement no-ops unless the stored value still\n")
            f.write("-- matches what was seen when this was generated. Review, then run.\n")
            f.write("BEGIN;\n")
            for (docid, before, after), n in sorted(by_doc.items()):
                f.write(f"UPDATE royalties SET commodity = {sql_array(list(after))}\n"
                        f" WHERE source_docid = '{docid.replace(chr(39), chr(39) * 2)}'\n"
                        f"   AND commodity IS NOT DISTINCT FROM {sql_array(list(before))};"
                        f"  -- {n} row(s)\n")
            f.write("COMMIT;\n")
        print(f"\nwrote {len(by_doc)} UPDATE statements ({len(planned)} rows) to {out}")
        return

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return

    snap = config.ROOT / f"data/_commodity_backfill_snapshot.csv"
    with snap.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "commodity_before", "commodity_after", "reason"])
        for rid, before, after, reason in planned:
            w.writerow([rid, json.dumps(before), json.dumps(after), reason])
    print(f"\nsnapshot of every change written to {snap}")

    with db.connect() as conn:
        with conn.cursor() as cur:
            for rid, _before, after, _reason in planned:
                cur.execute("update royalties set commodity = %s where id = %s", (after, rid))
        conn.commit()
    print(f"applied {len(planned)} updates")


if __name__ == "__main__":
    main()

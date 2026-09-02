"""Competitor-held flagging (Matt feedback, bucket B).

    python scripts/flag_competitors.py            # build the reviewable ledger (no DB writes)
    python scripts/flag_competitors.py --apply     # apply data/competitor_matches.json to the DB

Matches each instrument's holder against OR's competitor list (~/OR_Competitor_List_2026-09-01.xlsx —
24 royalty/streaming peers with aliases) and records which competitor, if any, holds it, in the derived
`competitor_holder` column. Approach (b): NON-DESTRUCTIVE — it never overwrites royalty_available or the
human score/review layer; it only sets a derived flag the UI surfaces. Whether a competitor-held row
should be forced 'not available' / score 0 is left for Matt to confirm before we wire it into scoring.

The matching is judgment (name variants, subsidiaries, holder DRIFT, mixed ownership), so Claude does it,
precision-favoring, with two hard rules:
  • OR Royalties / Osisko Gold Royalties (and subsidiaries) is the user's OWN company — never a competitor.
  • Use the CURRENT holder, then map it to the list. Royal Gold is now ON the list (with "Sandstorm" as
    an alias, since Royal Gold acquired Sandstorm), so a Royal Gold / Sandstorm holder -> "Royal Gold".
Same philosophy as scripts/resolve_holders.py: default writes only a ledger; nothing touches the DB until
--apply. The competitor file is confidential — it is read from $HOME and never committed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import anthropic  # noqa: E402
import pandas as pd  # noqa: E402

from techreport import config, db  # noqa: E402

LEDGER = config.ROOT / "data" / "competitor_matches.json"
COMPETITOR_XLSX = pathlib.Path.home() / "OR_Competitor_List_2026-09-01.xlsx"
MODEL = "claude-opus-5"
CHUNK = 120


def load_competitors() -> tuple[list[str], str]:
    """(canonical names, a reference block for the prompt) from the competitor xlsx."""
    df = pd.read_excel(COMPETITOR_XLSX, sheet_name="Competitors")
    names, lines = [], []
    for _, r in df.iterrows():
        name = str(r["Company"]).strip()
        names.append(name)
        aliases = str(r.get("Common aliases / notes", "") or "").strip()
        tic = str(r.get("Ticker", "") or "").strip()
        lines.append(f'- {name} (ticker {tic}) — aliases: {aliases}')
    return names, "\n".join(lines)


def system_prompt(ref: str) -> str:
    return f"""You flag which instruments in a royalty database are held by one of OR Royalties' COMPETITORS.

OR's competitor list (the ONLY companies that count as competitors):
{ref}

For each holder string, return the canonical competitor company name (exactly as listed above) if the
CURRENT holder is that competitor — otherwise null. Rules, precision-favoring (when unsure -> null):

1. OR ITSELF IS NOT A COMPETITOR. "OR Royalties", "Osisko Gold Royalties", "Osisko", "Osisko Bermuda",
   "OGR", "ORR", and any Osisko/OR subsidiary are the user's OWN company -> null. (Do not confuse
   "Osisko Gold Royalties" with the competitor "Gold Royalty Corp" — they are different companies.)
2. CURRENT holder only. Holder notes often describe drift/assignment. Use whoever holds it NOW, then
   match that current holder to the list (aliases fold acquired peers into their current owner):
   - "Royal Gold Inc. (formerly Sandstorm Gold Ltd.)" -> current is Royal Gold -> "Royal Gold".
   - "Sandstorm Gold Ltd." -> Sandstorm was acquired by Royal Gold and is a Royal Gold alias -> "Royal Gold".
   - "Maverix Metals" -> acquired by Triple Flag, a listed Triple Flag alias -> "Triple Flag Precious Metals".
3. Match name variants / abbreviations / tickers / named subsidiaries of a listed competitor
   (e.g. "Franco-Nevada Canada Holdings Corp." -> "Franco-Nevada"; "Gold Royalty U.S. Corp." ->
   "Gold Royalty Corp"; "Royalty & Streaming Mexico SA (owned by Metalla)" -> "Metalla Royalty & Streaming").
4. MIXED ownership: if OR/Osisko is one of the holders, the instrument is effectively OR's -> null,
   even if a competitor co-holds. If the holders are a competitor plus a non-OR third party, return the
   competitor.
5. Only the 24 companies on the list (and their aliases) count. A royalty peer that is genuinely NOT on
   the list or an alias of one -> null. (Note: Royal Gold, Sailfish, and Versamet/Sandbox ARE now on the
   list — do not null them.)

Return one object per input, echoing the exact input in "holder"."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def classify(holders: list[str], ref: str) -> dict[str, str | None]:
    tool = {
        "name": "emit",
        "description": "Return the competitor match (or null) for every holder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "holder": {"type": "string"},
                            "competitor": {"type": ["string", "null"]},
                        },
                        "required": ["holder", "competitor"],
                    },
                }
            },
            "required": ["items"],
        },
    }
    out: dict[str, str | None] = {}
    sysp = system_prompt(ref)
    for i in range(0, len(holders), CHUNK):
        batch = holders[i:i + CHUNK]
        payload = "\n".join(f"{j+1}. {h}" for j, h in enumerate(batch))
        msg = _client().messages.create(
            model=MODEL, max_tokens=8000, system=sysp, tools=[tool],
            tool_choice={"type": "tool", "name": "emit"},
            messages=[{"role": "user", "content": f"Classify these {len(batch)} holders:\n\n{payload}"}],
        )
        items = next((b.input["items"] for b in msg.content if b.type == "tool_use"), [])
        for it in items:
            out[it["holder"]] = it["competitor"] or None
        print(f"  classified {min(i + CHUNK, len(holders))}/{len(holders)}")
    return out


def build() -> None:
    names, ref = load_competitors()
    canon = set(names)
    with db.connect() as conn:
        rows = conn.execute(
            "select distinct holder from royalties where holder is not null order by 1"
        ).fetchall()
    holders = [r[0] for r in rows]
    print(f"distinct holders: {len(holders)}  |  competitors on list: {len(names)}")

    matched = classify(holders, ref)
    missing = [h for h in holders if h not in matched]
    if missing:
        print(f"  retrying {len(missing)} the model dropped...")
        matched.update(classify(missing, ref))

    ledger, unknown_labels = [], set()
    for h in holders:
        c = matched.get(h)
        if c and c not in canon:
            unknown_labels.add(c)  # model returned a name not on the list -> flag, don't trust
            c = None
        ledger.append({"holder": h, "competitor": c})

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    hits = [e for e in ledger if e["competitor"]]
    print(f"wrote {LEDGER}  ({len(ledger)} holders, {len(hits)} matched to a competitor)")
    from collections import Counter
    print("  by competitor:", dict(Counter(e["competitor"] for e in hits)))
    if unknown_labels:
        print(f"  ⚠ model returned non-listed names (set to null, review): {sorted(unknown_labels)}")
    print("\nReview data/competitor_matches.json, then:  python scripts/flag_competitors.py --apply")


def apply() -> None:
    if not LEDGER.exists():
        sys.exit("no ledger — run without --apply first, then review it.")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            cur.execute("update royalties set competitor_holder = null")  # recompute from scratch
            n = 0
            for e in ledger:
                if e["competitor"]:
                    cur.execute(
                        "update royalties set competitor_holder=%s where holder=%s",
                        (e["competitor"], e["holder"]),
                    )
                    n += cur.rowcount
        finally:
            cur.execute("alter table royalties enable trigger trg_roy_touch")
        conn.commit()
        print(f"flagged {n} rows as competitor-held.")
        print("\nby competitor (rows):")
        for r in cur.execute(
            "select competitor_holder, count(*) from royalties where competitor_holder is not null "
            "group by 1 order by 2 desc"
        ).fetchall():
            print("  ", *r)
        print("\nsanity — any OR/Osisko wrongly flagged? (should be 0):")
        r = cur.execute(
            "select count(*) from royalties where competitor_holder is not null "
            "and holder ~* 'osisko|or royalties|^orr'"
        ).fetchone()
        print("  ", r[0])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="apply the ledger to the DB (default: build ledger only)")
    args = ap.parse_args()
    apply() if args.apply else build()

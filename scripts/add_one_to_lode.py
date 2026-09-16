"""Extract royalties from ONE MarketWatch PR and insert them into a LODE database.

Backs the dashboard "Add to LODE" button. Reads a JSON object (argv[1] = a file path, or stdin) —
{docid, company, date, url, text} — extracts royalties with royalty.extract, inserts each into
`royalties` (ingested_from='marketwatch', status='pending', is_primary=true) in the DATABASE_URL DB,
and prints a JSON result. Point DATABASE_URL at lode_test for the local demo; nothing else is touched.

    DATABASE_URL=postgresql://lode:lode@localhost:5433/lode_test \
      python scripts/add_one_to_lode.py story.json
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import db, royalty  # noqa: E402

NAME2SYM = {"gold": "Au", "silver": "Ag", "copper": "Cu", "molybdenum": "Mo", "moly": "Mo",
            "nickel": "Ni", "zinc": "Zn", "lead": "Pb", "cobalt": "Co", "uranium": "U",
            "platinum": "PGE", "palladium": "PGE", "pge": "PGE", "pgm": "PGE", "iron": "Fe",
            "vanadium": "V", "lithium": "Li", "tin": "Sn", "tungsten": "W", "graphite": "C"}


def commodities(s: str | None) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"[,/&]|\band\b", (s or "")):
        t = tok.strip()
        if not t:
            continue
        sym = NAME2SYM.get(t.lower())
        if sym:
            out.append(sym)
        elif 1 <= len(t) <= 4 and t[0].isupper():
            out.append(t)
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def rate_pct(s: str | None) -> float | None:
    if not s or "%" not in s:
        return None
    m = re.search(r"[\d.]+", s)
    if not m:
        return None
    v = float(m.group())
    return v if v <= 25 else None


INSERT = """
INSERT INTO royalties
 (project_name, operator, commodity, jurisdiction, stage,
  royalty_type, rate, rate_pct, holder, royalty_available,
  partial_coverage, advance_payments, production_threshold, production_cap, buyback, step_down, rofr, features_note,
  regime, source_docid, source_label, source_url, source_date, source_quote, quote_verified,
  status, ingested_from, is_primary)
 VALUES (%(project_name)s,%(operator)s,%(commodity)s,%(jurisdiction)s,%(stage)s,
  %(royalty_type)s,%(rate)s,%(rate_pct)s,%(holder)s,'unknown',
  %(partial_coverage)s,%(advance_payments)s,%(production_threshold)s,%(production_cap)s,%(buyback)s,%(step_down)s,%(rofr)s,%(features_note)s,
  %(regime)s,%(source_docid)s,%(source_label)s,%(source_url)s,%(source_date)s,%(source_quote)s,%(quote_verified)s,
  'pending','marketwatch',true)
 ON CONFLICT (source_docid, project_name, holder, royalty_type) DO NOTHING
"""


def main() -> None:
    raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else sys.stdin.read()
    rec = json.loads(raw)
    text = rec.get("text") or ""
    ntext = re.sub(r"\s+", " ", text).strip().lower()

    passages = royalty.royalty_passages(text)
    if not passages:
        print(json.dumps({"inserted": 0, "reason": "no royalty passage found in this release"}))
        return

    ex = royalty.extract(passages, operator_hint=rec.get("company") or None)
    rows = []
    for r in ex.royalties:
        rows.append({
            "project_name": ex.project_name or rec.get("company") or "?",
            "operator": rec.get("company"),
            "commodity": commodities(ex.commodity),
            "jurisdiction": ex.jurisdiction,
            "stage": ex.stage,
            "royalty_type": r.royalty_type,
            "rate": r.rate,
            "rate_pct": rate_pct(r.rate),
            "holder": r.holder,
            "partial_coverage": r.partial_coverage,
            "advance_payments": r.advance_payments,
            "production_threshold": r.production_threshold,
            "production_cap": r.production_cap,
            "buyback": r.buyback,
            "step_down": r.step_down,
            "rofr": r.rofr,
            "features_note": getattr(r, "other_terms", None),
            "regime": "MarketWatch",
            "source_docid": rec.get("docid"),
            "source_label": "MarketWatch · " + (rec.get("date") or ""),
            "source_url": rec.get("url"),
            "source_date": rec.get("date"),
            "source_quote": re.sub(r"</?b>", "", r.quote or ""),
            "quote_verified": bool(r.quote) and re.sub(r"\s+", " ", (r.quote or "")).strip().lower()[:80] in ntext,
        })

    if rows:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(INSERT, rows)
            conn.commit()
    print(json.dumps({
        "inserted": len(rows),
        "project": ex.project_name,
        "royalties": [{"type": r.royalty_type, "rate": r.rate, "holder": r.holder} for r in ex.royalties],
    }))


if __name__ == "__main__":
    main()

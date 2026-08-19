"""Load the extraction pilot (data/royalty_pilot.json) into the `royalties` table.

    python scripts/load_royalties.py

Each extracted royalty becomes one row (status='pending'). Availability + the human/score fields are
left blank on purpose — they're analyst judgment, not in the report. After load, marks the newest source
per (asset, holder, type) as is_primary so the grid can default to one row per asset-royalty.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, db  # noqa: E402

PILOT = config.ROOT / "data" / "royalty_pilot.json"
MANIFEST = config.CORPUS_DIR / "_archive_manifest.json"

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
        elif 1 <= len(t) <= 4 and t[0].isupper():   # already a symbol like Au / PGE
            out.append(t)
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def rate_pct(s: str | None) -> float | None:
    """Leading percentage value for sort/filter — ONLY when the rate is expressed as a % (a
    US$/oz or one-time $ figure must not masquerade as a rate)."""
    if not s or "%" not in s:
        return None
    m = re.search(r"[\d.]+", s)
    if not m:
        return None
    v = float(m.group())
    return v if v <= 25 else None   # a real NSR/GSR/NPI rate; excludes "100%"/"20% of price" streams


def buckets(cond: str | None):
    """Rough map of the pilot's single free-text `conditions` into feature fields (until the schema
    upgrade extracts them structurally). Keeps the raw text in features_note regardless."""
    c = (cond or "").lower()
    return {
        "buyback": cond if ("buy" in c or "buy-down" in c or "buyback" in c) else None,
        "step_down": cond if ("sliding" in c or "step" in c) else None,
        "production_cap": cond if "cap" in c else None,
        "partial_coverage": True if ("partial" in c or "area" in c) else None,
    }


INSERT = """
INSERT INTO royalties
 (project_name, operator, commodity, jurisdiction, stage,
  royalty_type, rate, rate_pct, holder, holder_note, royalty_available, extract_confidence,
  buyback, step_down, production_cap, partial_coverage, features_note,
  regime, source_docid, source_label, source_url, source_date, source_quote, quote_verified,
  status, ingested_from)
 VALUES (%(project_name)s,%(operator)s,%(commodity)s,%(jurisdiction)s,%(stage)s,
  %(royalty_type)s,%(rate)s,%(rate_pct)s,%(holder)s,%(holder_note)s,'unknown',%(conf)s,
  %(buyback)s,%(step_down)s,%(production_cap)s,%(partial_coverage)s,%(features_note)s,
  %(regime)s,%(source_docid)s,%(source_label)s,%(source_url)s,%(source_date)s,%(source_quote)s,%(quote_verified)s,
  'pending','pilot')
 ON CONFLICT (source_docid, project_name, holder, royalty_type) DO NOTHING
"""

PRIMARY = """
UPDATE royalties r SET is_primary = FALSE
WHERE ingested_from = 'pilot' AND EXISTS (
  SELECT 1 FROM royalties r2 WHERE r2.ingested_from='pilot'
    AND lower(r2.project_name)=lower(r.project_name)
    AND coalesce(lower(r2.holder),'')=coalesce(lower(r.holder),'')
    AND coalesce(r2.royalty_type,'')=coalesce(r.royalty_type,'')
    AND (r2.source_date > r.source_date
         OR (r2.source_date IS NOT DISTINCT FROM r.source_date AND r2.id > r.id)));
"""

pilot = json.loads(PILOT.read_text(encoding="utf-8"))
url_by_doc = {}
if MANIFEST.exists():
    # the archive manifest carries an EDGAR archive URL for the S-K 1300 rows; join it in where present
    url_by_doc = {m["docid"]: m.get("url") for m in json.loads(MANIFEST.read_text()) if m.get("docid")}

rows = []
for rec in pilot:
    if not rec.get("has_third_party_royalty") or not rec.get("royalties"):
        continue
    for roy in rec["royalties"]:
        b = buckets(roy.get("conditions"))
        rows.append({
            "project_name": rec.get("project_name") or rec.get("operator") or "?",
            "operator": rec.get("operator"),
            "commodity": commodities(rec.get("commodity")),
            "jurisdiction": rec.get("jurisdiction"),
            "stage": rec.get("stage"),
            "royalty_type": roy.get("royalty_type"),
            "rate": roy.get("rate"),
            "rate_pct": rate_pct(roy.get("rate")),
            "holder": roy.get("holder"),
            "holder_note": None,
            "conf": None,
            "buyback": b["buyback"], "step_down": b["step_down"],
            "production_cap": b["production_cap"], "partial_coverage": b["partial_coverage"],
            "features_note": roy.get("conditions"),
            "regime": rec.get("regime"),
            "source_docid": rec.get("docid"),
            "source_label": f"{rec.get('regime')} · {rec.get('date')}" if rec.get("date") else rec.get("regime"),
            "source_url": url_by_doc.get(rec.get("docid")),
            "source_date": rec.get("date"),
            "source_quote": re.sub(r"</?b>", "", roy.get("quote") or ""),
            "quote_verified": bool(roy.get("quote_verified")),
        })

with db.connect() as conn:
    with conn.cursor() as cur:
        cur.executemany(INSERT, rows)
        inserted = cur.rowcount
        cur.execute(PRIMARY)
        cur.execute("SELECT count(*), count(*) FILTER (WHERE is_primary) FROM royalties WHERE ingested_from='pilot'")
        total, primary = cur.fetchone()
    conn.commit()

print(f"prepared {len(rows)} royalties -> loaded {total} rows ({primary} primary after dedup)")

"""Ingest a MarketWatch royalty_change export into a reviewable royalty ledger for LODE (Matt's C-11).

Mirrors ingest_edgar.py, but sources MarketWatch press-release intelligence instead of technical reports:
for each royalty_change story, re-extract the source PR text into a LODE royalty instrument. Writes
data/marketwatch_royalties.json with ingested_from='marketwatch'. It does NOT touch the database — a
human validates the ledger, then `load_royalties.py --source marketwatch` applies it, then dedupe.py
merges any overlap with the technical-report-sourced rows.

Input = a JSON array exported from MarketWatch (a read-only SELECT in Cloud Shell), one object per
royalty_change story:
    {docid, company, date, url, text}   # text = PR body_text; angle/trigger_text used as a fallback

    python scripts/ingest_marketwatch.py --mw-file mw_royalty_changes.json [--limit N]

Note: news announces deals that may not close; every row lands status='pending' with a marketwatch badge,
so the analyst decides at review time whether an announced royalty counts. That policy lives in the review,
not here.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, royalty  # noqa: E402

OUT = config.ROOT / "data" / "marketwatch_royalties.json"

ap = argparse.ArgumentParser()
ap.add_argument("--mw-file", required=True, help="JSON export of MarketWatch royalty_change stories")
ap.add_argument("--limit", type=int, default=None)
args = ap.parse_args()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def source_text(rec: dict) -> str:
    """Prefer the full PR body; fall back to the story angle / trigger snippet."""
    return rec.get("text") or rec.get("body_text") or rec.get("angle") or rec.get("trigger_text") or ""


results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
done = {r["docid"] for r in results}
stories = json.loads(pathlib.Path(args.mw_file).read_text(encoding="utf-8"))
print(f"MarketWatch export: {len(stories)} royalty_change stories; {len(done)} already ingested.")

n_new = 0
for st in stories:
    docid = st.get("docid") or st.get("document_id")
    if not docid or docid in done:
        continue
    if args.limit is not None and n_new >= args.limit:
        break
    operator = (st.get("company") or st.get("company_name") or "").strip()
    rec: dict = {"operator": operator, "regime": "MarketWatch", "source": "marketwatch",
                 "date": (st.get("date") or st.get("published_at") or "")[:10] or None,
                 "docid": docid, "url": st.get("url"), "ingested_from": "marketwatch"}
    text = source_text(st)
    if not text.strip():
        rec.update(status="no_text", has_third_party_royalty=False, royalties=[])
    else:
        ntext = norm(text)
        passages = royalty.royalty_passages(text)
        if not passages:
            rec.update(status="no_passages", has_third_party_royalty=False, royalties=[])
        else:
            try:
                ex = royalty.extract(passages, operator_hint=operator or None)
                roys = []
                for r in ex.royalties:
                    d = r.model_dump()
                    d["quote_verified"] = bool(r.quote) and norm(r.quote)[:80] in ntext
                    roys.append(d)
                rec.update(status="ok", project_name=ex.project_name, commodity=ex.commodity,
                           jurisdiction=ex.jurisdiction, stage=ex.stage,
                           has_third_party_royalty=ex.has_third_party_royalty,
                           royalties=roys, notes=ex.notes)
            except Exception as exc:  # noqa: BLE001 — one bad story never aborts the run
                rec.update(status="error", error=f"{type(exc).__name__}: {exc}"[:200])
    results.append(rec)
    done.add(docid)
    n_new += 1
    nroy = len(rec.get("royalties") or [])
    print(f"  [{n_new:4d}] {rec['status']:11s} {operator[:26]:26s} {rec['date']}  royalties={nroy}")
    if n_new % 10 == 0:
        OUT.write_text(json.dumps(results, indent=1))

OUT.write_text(json.dumps(results, indent=1))
print(f"\nDONE. {len(results)} MarketWatch stories in ledger -> {OUT}")

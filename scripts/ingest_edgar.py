"""Ingest the local SEC EDGAR S-K 1300 corpus into a reviewable royalty ledger for LODE.

Mirrors royalty_pilot.py, but sources the ~890 EX-96 Technical Report Summaries already downloaded to
C:\\EDGAR_Corpus (index.csv), converts each HTML/PDF to text, extracts third-party royalties, and writes
them to data/edgar_royalties.json. It does NOT touch the database — a human validates the ledger first,
then load_royalties applies it (per CLAUDE.md's reviewable-ledger rule).

    python scripts/ingest_edgar.py --limit 10     # validate a batch first
    python scripts/ingest_edgar.py                # full run (resumable; skips docs already done)

Cost note: royalty_passages caps the text window, and reports with no royalty passage take no Claude
call at all, so a batch is cheap. Per-report errors are isolated (one bad doc never aborts the run).
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, royalty  # noqa: E402
from techreport.archive import _html_text, _pdf_text  # noqa: E402

EDGAR_DIR = pathlib.Path("/mnt/c/EDGAR_Corpus")
INDEX = EDGAR_DIR / "index.csv"
OUT = config.ROOT / "data" / "edgar_royalties.json"

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=None)
args = ap.parse_args()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def clean_company(c: str) -> str:
    """'BHP Group Ltd  (BHP, BHPLF)  (CIK 0000811809)' -> 'BHP Group Ltd'."""
    return re.sub(r"\s*\(.*$", "", (c or "")).strip() or (c or "").strip()


def read_text(local_path: str) -> str:
    """EDGAR EX-96 is HTML (bs4) or occasionally PDF (fitz)."""
    p = pathlib.Path(local_path)
    if not p.exists() or not local_path:
        return ""
    raw = p.read_bytes()
    if p.suffix.lower() == ".pdf" or raw[:5] == b"%PDF-":
        return _pdf_text(str(p))
    return _html_text(raw)


results = json.loads(OUT.read_text()) if OUT.exists() else []
done = {r["docid"] for r in results}
rows = list(csv.DictReader(INDEX.open(encoding="utf-8")))
print(f"EDGAR corpus: {len(rows)} docs in index; {len(done)} already ingested.")

n_new = 0
for row in rows:
    docid = row.get("doc_id")
    if not docid or docid in done:
        continue
    if args.limit is not None and n_new >= args.limit:
        break
    operator = clean_company(row.get("company", ""))
    rec: dict = {"operator": operator, "regime": "S-K 1300", "source": "sec_edgar",
                 "date": row.get("file_date"), "docid": docid, "url": row.get("url"),
                 "ingested_from": "edgar"}
    text = read_text(row.get("local_path", ""))
    if not text.strip():
        rec.update(status="no_text", has_third_party_royalty=False, royalties=[])
    else:
        ntext = norm(text)
        passages = royalty.royalty_passages(text)
        if not passages:
            rec.update(status="no_passages", has_third_party_royalty=False, royalties=[])
        else:
            try:
                ex = royalty.extract(passages, operator_hint=operator)
                roys = []
                for r in ex.royalties:
                    d = r.model_dump()
                    d["quote_verified"] = bool(r.quote) and norm(r.quote)[:80] in ntext
                    roys.append(d)
                rec.update(status="ok", project_name=ex.project_name, commodity=ex.commodity,
                           jurisdiction=ex.jurisdiction, stage=ex.stage,
                           has_third_party_royalty=ex.has_third_party_royalty,
                           royalties=roys, notes=ex.notes)
            except Exception as exc:  # noqa: BLE001 — one bad report never aborts the run
                rec.update(status="error", error=f"{type(exc).__name__}: {exc}"[:200])
    results.append(rec)
    done.add(docid)
    n_new += 1
    nroy = len(rec.get("royalties") or [])
    print(f"  [{n_new:4d}] {rec['status']:11s} {operator[:26]:26s} {rec['date']}  royalties={nroy}")
    if n_new % 10 == 0:
        OUT.write_text(json.dumps(results, indent=1))

OUT.write_text(json.dumps(results, indent=1))
print(f"\nDONE. {len(results)} EDGAR reports in ledger -> {OUT}")

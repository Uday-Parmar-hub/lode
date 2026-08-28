"""Pilot: extract third-party royalties from every archived report (text) into data/royalty_pilot.json.

    python scripts/royalty_pilot.py [--limit N]

Resumable (skips reports already recorded), per-report error isolation. Reports with no royalty-related
passage are marked has_third_party_royalty=false WITHOUT a Claude call (saves cost). Every extracted
royalty is quote-verified against the source text so the hallucination rate is measurable.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config, royalty  # noqa: E402

CORPUS = config.CORPUS_DIR
MAN = CORPUS / "_archive_manifest.json"
OUT = config.ROOT / "data" / "royalty_pilot.json"

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=None)
args = ap.parse_args()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


results = json.loads(OUT.read_text()) if OUT.exists() else []
done = {r["docid"] for r in results}
archived = [m for m in json.loads(MAN.read_text()) if m.get("txt") and m.get("status") in ("ok", "partial")]

n_new = 0
for m in archived:
    if m["docid"] in done:
        continue
    if args.limit is not None and n_new >= args.limit:
        break
    text = (CORPUS / m["txt"]).read_text(errors="ignore")
    ntext = norm(text)
    rec = {k: m.get(k) for k in ("operator", "regime", "date", "docid", "txt")}
    passages = royalty.royalty_passages(text)
    if not passages:
        rec.update(status="no_passages", has_third_party_royalty=False, royalties=[])
    else:
        try:
            ex = royalty.extract(passages, operator_hint=m["operator"])
            roys = []
            for r in ex.royalties:
                d = r.model_dump()
                d["quote_verified"] = bool(r.quote) and norm(r.quote)[:80] in ntext
                roys.append(d)
            rec.update(status="ok", project_name=ex.project_name, commodity=ex.commodity,
                       jurisdiction=ex.jurisdiction, stage=ex.stage,
                       has_third_party_royalty=ex.has_third_party_royalty, royalties=roys, notes=ex.notes)
        except Exception as exc:  # noqa: BLE001 — one bad report never aborts the pilot
            rec.update(status="error", error=f"{type(exc).__name__}: {exc}"[:200])
    results.append(rec)
    done.add(m["docid"])
    n_new += 1
    nroy = len(rec.get("royalties") or [])
    print(f"  [{n_new:4d}] {rec['status']:11s} {m['operator'][:24]:24s} {m['date']}  royalties={nroy}")
    if n_new % 10 == 0:
        OUT.write_text(json.dumps(results, indent=1))

OUT.write_text(json.dumps(results, indent=1))
print(f"\nDONE. {len(results)} reports processed -> {OUT}")

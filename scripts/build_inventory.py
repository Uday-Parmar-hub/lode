"""Build the corpus inventory (technical reports per resolved operator, all regimes).

    python scripts/build_inventory.py
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import inventory  # noqa: E402

inv = [x for x in inventory.build_inventory() if "error" not in x]
with_reports = [x for x in inv if x["report_count"] > 0]
total = sum(x["report_count"] for x in inv)
oldest = min((x["oldest"] for x in with_reports if x["oldest"]), default="-")

by_regime = collections.Counter()
ops_by_regime = collections.Counter()
for x in inv:
    for regime, n in (x.get("by_regime") or {}).items():
        by_regime[regime] += n
        ops_by_regime[regime] += 1

print("\n=== CORPUS INVENTORY (all regimes) ===")
print(f"operators inventoried: {len(inv)}   with >=1 report: {len(with_reports)}   "
      f"total reports: {total}   oldest: {oldest}")
print("\nby regime:")
for regime, n in by_regime.most_common():
    print(f"  {regime:12s} {n:4d} reports across {ops_by_regime[regime]:2d} operators")

capped = [x for x in inv if x.get("capped_jorc")]
if capped:
    print(f"\nAU/JORC history capped (200-announcement limit) for {len(capped)} operator(s): "
          + ", ".join(x["operator"] for x in capped))

edgar_ops = [x for x in with_reports if "S-K 1300" in (x.get("by_regime") or {})]
if edgar_ops:
    print("\nUS S-K 1300 (SEC EDGAR EX-96) operators:")
    for x in sorted(edgar_ops, key=lambda x: x["by_regime"]["S-K 1300"], reverse=True):
        print(f"  {x['by_regime']['S-K 1300']:3d}  {x['operator'][:34]:34s} (CIK {x.get('cik')})")

print("\ntop operators by total report count:")
for x in sorted(with_reports, key=lambda x: x["report_count"], reverse=True)[:15]:
    regimes = "+".join(sorted(x["by_regime"]))
    print(f"  {x['report_count']:3d}  {x['operator'][:32]:32s} {x['oldest']}..{x['newest']}  [{regimes}]")

"""Build the corpus inventory (NI 43-101 technical reports per resolved operator).

    python scripts/build_inventory.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import inventory  # noqa: E402

inv = [x for x in inventory.build_inventory() if "error" not in x]
with_reports = [x for x in inv if x["report_count"] > 0]
total = sum(x["report_count"] for x in inv)
oldest = min((x["oldest"] for x in with_reports if x["oldest"]), default="-")

print(f"\n=== CORPUS INVENTORY ===")
print(f"operators inventoried: {len(inv)}   with >=1 report: {len(with_reports)}   "
      f"total reports: {total}   oldest: {oldest}")
print("\ntop operators by report count:")
for x in sorted(with_reports, key=lambda x: x["report_count"], reverse=True)[:15]:
    print(f"  {x['report_count']:3d}  {x['operator'][:34]:34s} {x['oldest']}..{x['newest']}  "
          f"({len(x['assets'])} portfolio asset(s))")

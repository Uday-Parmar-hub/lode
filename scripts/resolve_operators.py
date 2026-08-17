"""Resolve portfolio operators -> RIC -> LSEG PermID (+ history depth). Writes the manifest.

    python scripts/resolve_operators.py --sample 12    # test on a subset
    python scripts/resolve_operators.py                # full portfolio
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import resolve  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--sample", type=int, default=None)
args = ap.parse_args()

rows = resolve.resolve_operators(sample=args.sample)
status = collections.Counter(r.status for r in rows)
print("\nSTATUS:", dict(status), f"  (of {len(rows)})")
for r in sorted(rows, key=lambda r: r.status):
    tail = r.matched_name or r.proposed_note or ""
    print(f"  [{r.status:19s}] {r.operator[:30]:30s} ric={(r.proposed_ric or '-'):9s} "
          f"oldest={(r.oldest_filing or '-'):10s} {tail[:28]}")

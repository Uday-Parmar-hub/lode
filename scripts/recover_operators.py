"""Corpus recovery: re-resolve the failed operators with multi-candidate RICs. Updates the manifest.

    python scripts/recover_operators.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import recover  # noqa: E402

before = collections.Counter(r["status"] for r in json.load(open(recover._RES, encoding="utf-8")))
rows, n = recover.recover()
after = collections.Counter(r["status"] for r in rows)

print(f"\nRECOVERED {n} operators.")
print(f"before: {dict(before)}")
print(f"after : {dict(after)}")

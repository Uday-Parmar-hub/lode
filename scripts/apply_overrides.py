"""Apply manual identifier overrides (data/manual_overrides.json) to the resolution manifest.

    python scripts/apply_overrides.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import overrides  # noqa: E402

_, applied = overrides.apply_overrides()
print(f"Applied {len(applied)} override(s):\n")
for a in applied:
    ident = a["cik"] and f"CIK {a['cik']}" or a["permid"] or "(unresolved)"
    flag = "" if a["name_matched"] in (True, None) else f"  [name now '{a['matched_name']}']"
    print(f"  {a['operator'][:36]:36s} {str(a['ric'] or ''):9s} -> {ident}{flag}")

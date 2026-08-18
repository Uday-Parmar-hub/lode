"""Download the corpus inventory to disk (original document + extracted text), per operator.

    python scripts/archive_corpus.py                 # full run (resumable)
    python scripts/archive_corpus.py --limit 20      # first 20 not-yet-archived reports
    python scripts/archive_corpus.py --operator "McEwen Inc."   # one operator only

Resumable: re-running skips reports already recorded done in corpus/_archive_manifest.json.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import archive  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=None)
ap.add_argument("--operator", action="append", help="restrict to this operator (repeatable)")
args = ap.parse_args()

manifest = archive.archive_corpus(
    limit=args.limit,
    operators=set(args.operator) if args.operator else None,
)

status = collections.Counter(m.get("status") for m in manifest)
mb = sum(m.get("bytes", 0) for m in manifest) / 1e6
print(f"\n=== ARCHIVE MANIFEST ({len(manifest)} reports) ===")
print("  status:", dict(status))
print(f"  downloaded: {mb:.1f} MB")
errs = [m for m in manifest if m.get("status") == "error"]
if errs:
    print(f"  errors ({len(errs)}):")
    for m in errs[:10]:
        print(f"    {m.get('operator','?')[:26]:26s} {m.get('docid')}  {m.get('error')}")

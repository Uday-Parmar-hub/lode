"""Kscope depth-probe (v2 SEDAR listing): the real "as far back as we can go" answer.

The v3 search is unusable here (lossy resolver 404s many tickers; its date is Kscope's index date,
not the filing date). The v2 SEDAR listing resolves {ticker}:CA reliably and carries the true
date_filed, so we page it and count the NI 43-101 technical reports + their real date span.
"""
from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("SEDAR_MAX_PAGES", "40")  # page deep enough to see historical reports

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import config  # loads .env
from techreport.kscope_client import KscopeClient

CANDIDATES = [
    ("Island Gold", "Alamos Gold", "AGI"),
    ("Canadian Malartic / Odyssey / Macassa", "Agnico Eagle", "AEM"),
    ("various", "Kinross", "K"),
]


def _span(docs: list) -> str:
    dates = sorted(str(d.published_at)[:10] for d in docs if d.published_at)
    return f"{dates[0]} .. {dates[-1]}" if dates else "(no dates)"


def main() -> None:
    cli = KscopeClient(api_key=config.KSCOPE_API_KEY, base_url=config.KSCOPE_BASE_URL)
    for asset, op, tk in CANDIDATES:
        print(f"\n===== {op} ({tk}) — SEDAR NI 43-101 =====")
        reports, supporting = [], []
        try:
            for doc in cli.iter_documents_for_ticker(tk, doc_types=["ni43101", "ni43101_supporting"]):
                (reports if doc.doc_type == "ni43101" else supporting).append(doc)
        except Exception as exc:  # noqa: BLE001
            print(f"  error: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        print(f"  full technical reports: {len(reports)}   span {_span(reports)}")
        print(f"  supporting (consents/certs): {len(supporting)}")
        for d in sorted(reports, key=lambda d: d.published_at or __import__('datetime').datetime.min.replace(tzinfo=__import__('datetime').timezone.utc), reverse=True)[:8]:
            print(f"    {str(d.published_at)[:10]}  {(d.title or '')[:66]}")
    cli.close()


if __name__ == "__main__":
    main()

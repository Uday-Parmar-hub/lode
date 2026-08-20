"""Count the ALL-TIME universe of technical reports, per regime, by enumeration (not per-operator).

    python scripts/universe_count.py

Canada NI 43-101 (SEDAR via LSEG): query the "Technical Report (NI 43-101)" submission-type code with
NO company filter, over date windows. LSEG caps any query at 200 rows, so we adaptively bisect any window
that returns 200 until every leaf window is under the cap, then sum — an exact count, not an estimate.

US S-K 1300 (SEC EDGAR, free): the TRS is filed as an EX-96.x exhibit. EDGAR full-text search reports a
total for the phrase "technical report summary", but that also matches consents (EX-23) and 10-K bodies,
so we date-window the search (deep pagination 500s on efts) and count only EX-96 exhibits. S-K 1300 took
effect in 2021, so the window is 2021-> .

Australia JORC: NOT enumerable this way — JORC results are ASX *announcements*, not a filing type, so there
is no code/total to sweep; the LSEG CRIS feed is also forward-only + shallow. Reported as "not countable".

Measured 2026-08-20:  SEDAR 10,582 (2007–2026, exact) · US 893 (2021–2026) · AU n/a.
"""
from __future__ import annotations

import collections
import datetime as dt
import sys
import time

import httpx

sys.path.insert(0, "src")
from techreport.lseg import LSEG, token  # noqa: E402
from techreport.inventory import NI43101_SUBTYPE  # noqa: E402

_UA = {"User-Agent": "OR Royalties technical-report research (uparmar@orroyalties.com)"}
_EFTS = "https://efts.sec.gov/LATEST/search-index"
_TRS_PHRASE = '"technical report summary"'

CAP = 200
_state = {"cli": LSEG(), "q": 0}


def _refresh_if_needed() -> None:
    _state["q"] += 1
    if _state["q"] % 90 == 0:            # LSEG token lives ~5 min; re-auth well inside that
        _state["cli"] = LSEG(token())


def window_count(d_from: dt.date, d_to: dt.date) -> int:
    """Rows for [d_from .. d_to] inclusive (day granularity), capped at 200."""
    q = ('{ FinancialFiling(filter: {AND: ['
         '{FilingDocument: {DocumentSummary: {GlobalSubmissionTypeCode: {EQ: "%s"}}}},'
         '{FilingDocument: {DocumentSummary: {FilingDate: {BETWN: {FROM: "%sT00:00:00Z", TO: "%sT23:59:59Z"}}}}}'
         ']}, sort: {FilingDocument: {DocumentSummary: {FilingDate: DESC}}}, limit: %d) {'
         ' FilingDocument { DocId } } }') % (NI43101_SUBTYPE, d_from.isoformat(), d_to.isoformat(), CAP)
    _refresh_if_needed()
    rows = _state["cli"]._gql(q).get("FinancialFiling") or []  # noqa: SLF001
    return len(rows)


def exact_count(d_from: dt.date, d_to: dt.date) -> int:
    """Exact count over a range: bisect any window that hits the 200 cap into non-overlapping halves."""
    n = window_count(d_from, d_to)
    if n < CAP or d_from >= d_to:
        return n
    mid = d_from + (d_to - d_from) // 2
    return exact_count(d_from, mid) + exact_count(mid + dt.timedelta(days=1), d_to)


def _efts(params: dict, tries: int = 4) -> dict:
    for i in range(tries):
        try:
            r = httpx.get(_EFTS, params=params, headers=_UA, timeout=40.0)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError("5xx", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(0.6 * (i + 1))
    return {}


def us_ex96_count(y0: int = 2021, end: dt.date | None = None) -> tuple[int, dict]:
    """Exact EX-96 (S-K 1300 TRS) exhibit count via month-windowed EDGAR full-text search."""
    end = end or dt.date.today()
    seen: set[str] = set()
    by_year: collections.Counter = collections.Counter()
    y, m = y0, 1
    while dt.date(y, m, 1) <= end:
        nm = dt.date(y + (m // 12), (m % 12) + 1, 1)
        a, b = dt.date(y, m, 1), nm - dt.timedelta(days=1)
        frm, total = 0, None
        while True:
            j = _efts({"q": _TRS_PHRASE, "startdt": a.isoformat(), "enddt": b.isoformat(), "from": frm})
            hits = j.get("hits", {}).get("hits", [])
            if total is None:
                total = j.get("hits", {}).get("total", {}).get("value", 0)
            for h in hits:
                if str(h.get("_source", {}).get("file_type") or "").startswith("EX-96"):
                    seen.add(h.get("_id"))
                    by_year[(h["_source"].get("file_date") or "")[:4]] += 1
            frm += len(hits)
            if not hits or frm >= min(total, 900):
                break
            time.sleep(0.12)
        y, m = (y + (m // 12), (m % 12) + 1)
    return len(seen), dict(sorted(by_year.items()))


def main() -> None:
    today = dt.date(2026, 8, 20)

    print("Canada — SEDAR NI 43-101 technical reports (exact, no company filter)\n")
    sedar = 0
    for yr in range(2000, today.year + 1):
        start = dt.date(yr, 1, 1)
        end = dt.date(yr, 12, 31) if yr < today.year else today
        n = exact_count(start, end)
        sedar += n
        print(f"  {yr}: {n:>5}     (running {sedar:>6}, {_state['q']} queries)")

    print("\nUS — S-K 1300 EX-96 Technical Report Summaries (SEC EDGAR)\n")
    us, us_by_year = us_ex96_count(2021, today)
    print(f"  by year: {us_by_year}")

    print("\n" + "=" * 56)
    print(f"  Canada  NI 43-101 (SEDAR)   {sedar:>6}   2007–2026, exact")
    print(f"  US      S-K 1300 (EDGAR)    {us:>6}   2021–2026")
    print(f"  Aust.   JORC                   n/a   not a filing type — not enumerable")
    print(f"  {'ACCESSIBLE UNIVERSE':<27} {sedar + us:>6}")
    print("=" * 56)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()

"""US S-K 1300 technical reports via SEC EDGAR full-text search (free, no LSEG).

US-domestic issuers file their S-K 1300 Technical Report Summary as Exhibit 96.x to a 10-K / 20-F /
S-1 (etc.) — there is no standalone "technical report" filing type on EDGAR. But SEC full-text search
(efts.sec.gov) indexes each *exhibit* separately with its file_type ("EX-96.1"), so we can isolate the
TRS cleanly by keying on that. History effectively starts 2021, when S-K 1300 took effect (earlier US
disclosure used Industry Guide 7, which had no Technical Report Summary).

Two guards keep this honest, mirroring the LSEG side:
  - the ticker->CIK lookup is NAME-VERIFIED (SEC reuses tickers: "GORO" resolves to Goldgroup, not
    Gold Resource — rejected via the same _name_matches used for RICs);
  - paging is driven by the reported total, so every EX-96 exhibit is seen, not just the first page.
"""
from __future__ import annotations

import json
import time

import httpx

from . import config
from .resolve import _name_matches

_UA = "OR Royalties technical-report research (uparmar@orroyalties.com)"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
_CIK_CACHE = config.ROOT / "data" / "sec_cik_map.json"
_TRS_PHRASE = '"technical report summary"'  # the S-K 1300 exhibit's own heading
_MAX_HITS = 400          # ceiling on phrase-hits paged per operator (TRS filings are few)
_POLITE_SECS = 0.15      # SEC fair-access pause between requests

# RIC suffixes whose bare ticker is a US symbol worth an EDGAR lookup.
US_SUFFIXES = {"N", "O", "OQ", "K", "A", "P"}


def _headers() -> dict[str, str]:
    return {"User-Agent": _UA}


def load_cik_map() -> dict[str, dict]:
    """{TICKER: {"cik": "0000000000", "name": ...}} from SEC's ticker directory (cached to data/)."""
    if _CIK_CACHE.exists():
        return json.loads(_CIK_CACHE.read_text(encoding="utf-8"))
    raw = httpx.get(_TICKERS_URL, headers=_headers(), timeout=40.0).json()
    m = {v["ticker"].upper(): {"cik": f"{v['cik_str']:010d}", "name": v["title"]}
         for v in raw.values()}
    _CIK_CACHE.parent.mkdir(exist_ok=True)
    _CIK_CACHE.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return m


def ticker_from_ric(ric: str | None) -> tuple[str | None, bool]:
    """(bare ticker, is_us_symbol) from a RIC. 'BVN.N'->('BVN',True); 'USAU.O'->('USAU',True);
    'AAA'->('AAA',True) for bare US symbols; 'RMS.AX'->('RMS',False)."""
    if not ric:
        return None, False
    if "." not in ric:
        return ric.upper(), True
    base, suffix = ric.rsplit(".", 1)
    return base.upper(), suffix.upper() in US_SUFFIXES


def cik_for(ticker: str, operator: str, cikmap: dict[str, dict]) -> tuple[str | None, str | None]:
    """US ticker -> (CIK, SEC name), name-verified against the operator so ticker reuse is rejected."""
    row = cikmap.get(ticker.upper())
    if not row:
        return None, None
    if not _name_matches(operator, row["name"]):
        return None, row["name"]  # collision (e.g. GORO->Goldgroup) — surfaced, not trusted
    return row["cik"], row["name"]


def _archive_url(cik: str, accession: str, filename: str) -> str:
    """Public EDGAR archive URL for a filing document."""
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"


def _fts_page(cik: str, frm: int) -> dict:
    r = httpx.get(_FTS_URL, params={"q": _TRS_PHRASE, "ciks": cik, "from": frm},
                  headers=_headers(), timeout=40.0)
    r.raise_for_status()
    return r.json()


def technical_report_summaries(cik: str) -> list[dict]:
    """All S-K 1300 EX-96.x Technical Report Summaries for a CIK (newest first)."""
    out: dict[tuple[str, str], dict] = {}
    frm = 0
    total = None
    while frm < _MAX_HITS:
        j = _fts_page(cik, frm)
        hits = j.get("hits", {}).get("hits", [])
        if total is None:
            total = (j.get("hits", {}).get("total", {}) or {}).get("value", 0)
        if not hits:
            break
        for h in hits:
            s = h.get("_source", {})
            ft = str(s.get("file_type") or "")
            if not ft.startswith("EX-96"):
                continue
            accession, _, filename = (h.get("_id") or "").partition(":")
            key = (accession, ft)
            if key in out or not accession:
                continue
            out[key] = {
                "date": s.get("file_date"),
                "docid": h.get("_id"),          # accession:filename — the archiver's handle
                "exhibit": ft,
                "url": _archive_url(cik, accession, filename) if filename else None,
                "title": f"S-K 1300 Technical Report Summary ({ft})",
            }
        frm += len(hits)
        if total is not None and frm >= total:
            break
        time.sleep(_POLITE_SECS)
    return sorted(out.values(), key=lambda x: x.get("date") or "", reverse=True)

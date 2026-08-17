"""Corpus inventory: for each resolved operator, list its technical reports across every regime and
all available history (date, title, docid) — the 'corpus map' the archiver + database build on.

Three regimes, three mechanisms, one merged per-operator record:
  - Canada  NI 43-101  -> LSEG SEDAR, filtered by the submission-type code (full history, one query).
  - US      S-K 1300   -> SEC EDGAR full-text search, isolating the EX-96.x exhibits (2021-> ; free).
  - Australia JORC      -> LSEG CRIS announcements, title-filtered client-side (200-cap = ~1-3yr; the
                           deep AU back-catalogue is the known history gap, flagged per operator).

A company may appear in more than one regime (cross-listed issuers file both a SEDAR 43-101 and a
US S-K 1300 TRS for the same deposit); we keep both and tag each report with its `regime`/`source`, so
the counts stay transparent and asset-level de-duplication is deferred to extraction (the report title
is generic — which ASSET it covers comes from the content, not the metadata).

Writes data/corpus_inventory.json (gitignored).
"""
from __future__ import annotations

import json

from . import config, edgar, portfolio
from .asx_jorc import jorc_reports
from .lseg import LSEG

# SEDAR submission-type code for "Technical Report (NI 43-101)" (verified live across operators).
NI43101_SUBTYPE = "SD002002001114001003204"

_RES = config.ROOT / "data" / "operator_resolution.json"
_OUT = config.ROOT / "data" / "corpus_inventory.json"


def technical_reports(cli: LSEG, permid: str) -> list[dict]:
    """All NI 43-101 (SEDAR) technical reports for an org (newest first): [{date, docid, title}]."""
    query = (
        '{ FinancialFiling(filter: {AND: ['
        '{FilingDocument: {Identifiers: {OrganizationId: {EQ: "%s"}}}},'
        '{FilingDocument: {DocumentSummary: {GlobalSubmissionTypeCode: {EQ: "%s"}}}}'
        ']}, sort: {FilingDocument: {DocumentSummary: {FilingDate: DESC}}}, limit: 200) {'
        ' FilingDocument { DocId DocumentSummary { DocumentTitle FilingDate } } } }'
    ) % (permid, NI43101_SUBTYPE)
    out: list[dict] = []
    for r in cli._gql(query).get("FinancialFiling") or []:  # noqa: SLF001
        fd = r["FilingDocument"]["DocumentSummary"]
        out.append({
            "date": (fd.get("FilingDate") or "")[:10] or None,
            "docid": r["FilingDocument"]["DocId"],
            "title": fd.get("DocumentTitle"),
        })
    return out


def _tag(reports: list[dict], regime: str, source: str) -> list[dict]:
    return [{**r, "regime": regime, "source": source} for r in reports]


def _collect(cli: LSEG, r: dict, cikmap: dict[str, dict]) -> dict:
    """Gather every regime's reports for one resolved operator into a merged record."""
    name, permid, ric = r["operator"], r["permid"], r.get("proposed_ric")
    reports: list[dict] = _tag(technical_reports(cli, permid), "NI 43-101", "lseg_sedar")
    rec: dict = {"operator": name, "permid": permid, "ric": ric, "status": r["status"]}

    # US S-K 1300 — only when the RIC is a US symbol and the CIK name-verifies.
    ticker, is_us = edgar.ticker_from_ric(ric)
    if is_us and ticker:
        cik, sec_name = edgar.cik_for(ticker, name, cikmap)
        if cik:
            reports += _tag(edgar.technical_report_summaries(cik), "S-K 1300", "sec_edgar")
            rec["cik"] = cik
        elif sec_name:
            rec["edgar_note"] = f"ticker {ticker} -> '{sec_name}' (name mismatch, skipped)"

    # Australia JORC — CRIS announcements, title-filtered (history may be capped).
    if (ric or "").upper().endswith(".AX"):
        jorc, capped = jorc_reports(cli, permid)
        reports += _tag(jorc, "JORC", "lseg_cris")
        rec["capped_jorc"] = capped

    reports.sort(key=lambda x: x.get("date") or "", reverse=True)
    dates = sorted(x["date"] for x in reports if x.get("date"))
    by_regime: dict[str, int] = {}
    for x in reports:
        by_regime[x["regime"]] = by_regime.get(x["regime"], 0) + 1
    rec.update({
        "assets": [a.asset for a in portfolio.by_operator().get(name, [])],
        "report_count": len(reports),
        "by_regime": by_regime,
        "oldest": dates[0] if dates else None,
        "newest": dates[-1] if dates else None,
        "reports": reports,
    })
    return rec


def build_inventory() -> list[dict]:
    """For every resolved operator, inventory its reports across all regimes; write + return manifest."""
    resolutions = json.loads(_RES.read_text(encoding="utf-8"))
    resolved = [r for r in resolutions
                if r.get("permid") and r["status"] in ("resolved_full", "resolved_thin")]
    cikmap = edgar.load_cik_map()

    cli = LSEG()
    inv: list[dict] = []
    for i, r in enumerate(resolved, 1):
        if i % 40 == 0:
            cli = LSEG()  # refresh the ~5-min token part-way through
        try:
            inv.append(_collect(cli, r, cikmap))
        except Exception as exc:  # noqa: BLE001 — one bad operator must not abort the batch
            inv.append({"operator": r["operator"], "permid": r["permid"], "error": type(exc).__name__})

    _OUT.parent.mkdir(exist_ok=True)
    _OUT.write_text(json.dumps(inv, indent=1), encoding="utf-8")
    return inv

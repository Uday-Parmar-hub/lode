"""Corpus inventory: for each resolved operator, list its NI 43-101 technical reports across all
history (date, title, docid) — the 'corpus map' the archiver + database build on.

Efficient: LSEG can't filter by title and caps queries at 200, but SEDAR tags every NI 43-101 with
a submission-type code, which IS filterable — so one query returns an operator's full report history
(reports are few; well under the cap). Canadian (SEDAR 43-101) operators only for now; US (S-K 1300)
and Australian (JORC via the ASX source) reports use different mechanisms — a later pass.

Reports carry a generic title ("Technical report (NI 43-101) - English"), so which ASSET each covers
isn't in the metadata — that's resolved later from the report content. Here we inventory per operator
(with the operator's portfolio assets attached for context).

Writes data/corpus_inventory.json (gitignored).
"""
from __future__ import annotations

import json

from . import config, portfolio
from .lseg import LSEG

# SEDAR submission-type code for "Technical Report (NI 43-101)" (verified live across operators).
NI43101_SUBTYPE = "SD002002001114001003204"

_RES = config.ROOT / "data" / "operator_resolution.json"
_OUT = config.ROOT / "data" / "corpus_inventory.json"


def technical_reports(cli: LSEG, permid: str) -> list[dict]:
    """All NI 43-101 technical reports for an org (newest first): [{date, docid, title}]."""
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


def build_inventory() -> list[dict]:
    """For every resolved operator, inventory its NI 43-101 reports; write + return the manifest."""
    resolutions = json.loads(_RES.read_text(encoding="utf-8"))
    by_op = portfolio.by_operator()
    resolved = [r for r in resolutions
                if r.get("permid") and r["status"] in ("resolved_full", "resolved_thin")]

    cli = LSEG()
    inv: list[dict] = []
    for i, r in enumerate(resolved, 1):
        if i % 40 == 0:
            cli = LSEG()  # refresh the ~5-min token part-way through
        try:
            reports = technical_reports(cli, r["permid"])
        except Exception as exc:  # noqa: BLE001 — one bad operator must not abort the batch
            inv.append({"operator": r["operator"], "permid": r["permid"], "error": type(exc).__name__})
            continue
        dates = sorted(x["date"] for x in reports if x["date"])
        inv.append({
            "operator": r["operator"], "permid": r["permid"], "ric": r.get("proposed_ric"),
            "status": r["status"],
            "assets": [a.asset for a in by_op.get(r["operator"], [])],
            "report_count": len(reports),
            "oldest": dates[0] if dates else None,
            "newest": dates[-1] if dates else None,
            "reports": reports,
        })

    _OUT.parent.mkdir(exist_ok=True)
    _OUT.write_text(json.dumps(inv, indent=1), encoding="utf-8")
    return inv

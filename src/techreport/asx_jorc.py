"""Australian JORC technical/economic reports via LSEG CRIS announcements.

Unlike SEDAR (NI 43-101) and EDGAR (S-K 1300), JORC has no standalone filing type: resource/reserve
estimates, feasibility and scoping studies are ASX ANNOUNCEMENTS (LSEG feed 'CRIS'), and every
announcement carries the same generic code — so a report can only be isolated by its title, matched
client-side (LSEG can't filter on DocumentTitle).

Honest limitation: LSEG caps a query at 200 announcements. For very active issuers 200 announcements
reach back only ~1-3 years, so the deep JORC back-catalogue is NOT retrievable here (it isn't
retrievable from any programmatic source we have — this is the "as far back as we can go" gap). We
flag `capped` when the raw announcement stream hit the 200 limit, so the shortfall is never silent.
"""
from __future__ import annotations

import datetime as dt
import re

from .lseg import LSEG

_LIMIT = 200

# Titles that denote a JORC technical/economic report (resource/reserve estimate or study).
JORC_KEYWORDS = re.compile(
    r"ore reserve|mineral resource|jorc|feasibility study|pre-?feasibility|"
    r"\bpfs\b|\bdfs\b|\bbfs\b|scoping study|resource estimate|reserve estimate|"
    r"resource update|resource upgrade|mineral resource and ore reserve|\bpea\b|"
    r"preliminary economic|resource and reserve",
    re.I,
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jorc_reports(cli: LSEG, permid: str, since: str = "2008-01-01T00:00:00Z") -> tuple[list[dict], bool]:
    """(reports, capped): JORC resource/reserve/study announcements for an org, newest first.
    `capped` is True when the raw announcement stream hit the 200-row limit (history truncated)."""
    query = (
        '{ FinancialFiling(filter: {AND: ['
        '{FilingDocument: {Identifiers: {OrganizationId: {EQ: "%s"}}}},'
        '{FilingDocument: {DocumentSummary: {FeedName: {EQ: "CRIS"}}}},'
        '{FilingDocument: {DocumentSummary: {FilingDate: {BETWN: {FROM: "%s", TO: "%s"}}}}}'
        ']}, sort: {FilingDocument: {DocumentSummary: {FilingDate: DESC}}}, limit: %d) {'
        ' FilingDocument { DocId DocumentSummary { DocumentTitle FilingDate } } } }'
    ) % (permid, since, _now_iso(), _LIMIT)
    rows = cli._gql(query).get("FinancialFiling") or []  # noqa: SLF001
    out: list[dict] = []
    for r in rows:
        fd = r["FilingDocument"]["DocumentSummary"]
        title = fd.get("DocumentTitle") or ""
        if not JORC_KEYWORDS.search(title):
            continue
        out.append({
            "date": (fd.get("FilingDate") or "")[:10] or None,
            "docid": r["FilingDocument"]["DocId"],
            "title": title,
        })
    return out, len(rows) >= _LIMIT

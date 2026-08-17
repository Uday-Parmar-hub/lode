"""LSEG Global Filings client — auth, symbology, filings query, full text, PDF retrieval.

Every method here was proven live during the PoC (see docs/LSEG_Filings_Test_Report.md):
  - OAuth v1 password grant (the news app key carries the account's Filings entitlement)
  - symbology convert: ticker/RIC -> Organization PermID
  - GraphQL FinancialFiling filtered by OrganizationId (entity-scoped) + SEDAR feed + date
  - DocumentText: full extracted body (PRs, MD&A, financials, 43-101 technical reports)
  - FilesMetaData.FileLink: the original PDF / txt, downloadable with the bearer token
"""
from __future__ import annotations

import datetime as dt

import httpx

from . import config

TOKEN_URL = "https://api.refinitiv.com/auth/oauth2/v1/token"
SYMBOL_URL = "https://api.refinitiv.com/data/symbology/beta1/convert"
GQL = "https://api.refinitiv.com/data-store/v1/graphql"


def token() -> str:
    """OAuth access token (valid ~5 min)."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "password", "username": config.LSEG_USERNAME,
            "password": config.LSEG_PASSWORD, "scope": "trapi",
            "client_id": config.LSEG_APP_KEY, "takeExclusiveSignOnControl": "true",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


class LSEG:
    """Thin client over the RDP Global Filings GraphQL + retrieval endpoints."""

    def __init__(self, access_token: str | None = None) -> None:
        self.token = access_token or token()
        self._h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    # -- entity resolution -------------------------------------------------
    def org_permid(self, ric: str) -> str | None:
        """Ticker/RIC (e.g. 'SSRM.TO', 'WRLG.V') -> Organization PermID, or None."""
        r = httpx.get(f"{SYMBOL_URL}?universe={ric}",
                      headers={"Authorization": f"Bearer {self.token}"}, timeout=30.0).json()
        try:
            return r["universe"][0]["Organization PermID"]
        except (KeyError, IndexError, TypeError):
            return None

    # -- filings -----------------------------------------------------------
    def _gql(self, query: str) -> dict:
        r = httpx.post(GQL, headers=self._h, json={"query": query}, timeout=120.0).json()
        if r.get("errors"):
            raise RuntimeError(f"LSEG GraphQL error: {r['errors'][:2]}")
        return r["data"]

    def filings(self, permid: str, *, days: int = 420, title_contains: str | None = None,
                limit: int = 60) -> list[dict]:
        """SEDAR filings for an org (newest first). Each item is a FilingDocument dict."""
        now = dt.datetime.now(dt.timezone.utc)
        frm = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
        to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        query = (
            '{ FinancialFiling(filter: {AND: ['
            '{FilingDocument: {Identifiers: {OrganizationId: {EQ: "%s"}}}},'
            '{FilingDocument: {DocumentSummary: {FeedName: {EQ: "SEDAR"}}}},'
            '{FilingDocument: {DocumentSummary: {FilingDate: {BETWN: {FROM: "%s", TO: "%s"}}}}}'
            ']}, sort: {FilingDocument: {DocumentSummary: {FilingDate: DESC}}}, limit: %d) {'
            ' FilingDocument { DocId DocumentSummary { DocumentTitle FormType FilingDate HighLevelCategory } } } }'
        ) % (permid, frm, to, limit)
        docs = [row["FilingDocument"] for row in (self._gql(query)["FinancialFiling"] or [])]
        if title_contains:
            needle = title_contains.lower()
            docs = [d for d in docs if needle in (d["DocumentSummary"].get("DocumentTitle") or "").lower()]
        return docs

    def latest(self, permid: str, title_contains: str, *, days: int = 420) -> dict | None:
        """Most recent filing whose title contains e.g. 'MD&A', 'Interim financial', 'Technical report'."""
        found = self.filings(permid, days=days, title_contains=title_contains)
        return found[0] if found else None

    # -- content -----------------------------------------------------------
    def document_text(self, docid: str) -> str:
        """Full extracted body text for a filing (empty string if none)."""
        query = ('{ FinancialFiling(filter: {FilingDocument: {DocId: {EQ: "%s"}}}, limit: 1) {'
                 ' FilingDocument { DocumentText } } }') % docid
        rows = self._gql(query)["FinancialFiling"] or []
        return (rows[0]["FilingDocument"].get("DocumentText") or "") if rows else ""

    def files(self, docid: str) -> list[dict]:
        """FilesMetaData: [{FileName, MimeType, FileLink}, ...] — incl. the original PDF."""
        query = ('{ FinancialFiling(filter: {FilingDocument: {DocId: {EQ: "%s"}}}, limit: 1) {'
                 ' FilingDocument { FilesMetaData { FileName MimeType FileLink } } } }') % docid
        rows = self._gql(query)["FinancialFiling"] or []
        return (rows[0]["FilingDocument"].get("FilesMetaData") or []) if rows else []

    def pdf_link(self, docid: str) -> str | None:
        """The application/pdf FileLink for a filing, or None."""
        for f in self.files(docid):
            if (f.get("MimeType") or "").lower() == "application/pdf":
                return f.get("FileLink")
        return None

    def download(self, file_link: str, dest: str) -> str:
        """Stream a FileLink (e.g. the raw PDF) to disk. Bearer token is enough."""
        with httpx.stream("GET", file_link, headers={"Authorization": f"Bearer {self.token}"},
                          timeout=120.0, follow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(65536):
                    fh.write(chunk)
        return dest


def fetch_latest(ticker: str, title_contains: str = "MD&A", *, days: int = 420) -> dict:
    """Latest SEDAR filing whose title contains ``title_contains`` for a RIC (e.g. 'SSRM.TO').

    Shares the return shape with ``kscope.fetch_latest`` so scripts can be source-agnostic:
    {title, filed, text, docid, source, parse}.
    """
    cli = LSEG()
    permid = cli.org_permid(ticker)
    if not permid:
        raise RuntimeError(f"could not resolve {ticker} via LSEG symbology")
    doc = cli.latest(permid, title_contains, days=days)
    if not doc:
        raise RuntimeError(f"no '{title_contains}' filing for {ticker} via LSEG in {days}d")
    ds = doc["DocumentSummary"]
    return {
        "title": ds["DocumentTitle"],
        "filed": ds["FilingDate"][:10],
        "text": cli.document_text(doc["DocId"]),
        "docid": doc["DocId"],
        "source": "LSEG Global Filings (SEDAR)",
        "parse": "DocumentText (ready)",
    }

"""Operator identifier resolution: portfolio operator name -> RIC -> LSEG PermID (+ history check).

LSEG can't resolve free-text names, only tickers, so we do a robust two-step:
  1. Claude proposes a RIC (ticker+exchange) for each operator — a knowledgeable guess.
  2. LSEG symbology VALIDATES it (resolves the RIC to a PermID + the company's common name); a wrong
     guess simply fails to resolve or name-mismatches, so hallucinations are caught, not trusted.
Then we check each PermID's filing-history depth (oldest filing) to flag entity quirks like Alamos,
where a ticker resolved to a recent PermID with only ~1yr of history.

Writes data/operator_resolution.json (gitignored — derived from real portfolio data).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass

import anthropic

from . import config, portfolio
from .lseg import LSEG, SYMBOL_URL

import httpx  # noqa: E402  (after local imports for readability)

_OUT = config.ROOT / "data" / "operator_resolution.json"
_PROPOSE_MODEL = "claude-sonnet-4-6"

_PROMPT = """You are mapping mining-company OPERATOR NAMES to their primary stock-exchange listing,
as an LSEG/Refinitiv RIC (ticker + exchange suffix). Common suffixes: TSX=.TO, TSXV=.V, ASX=.AX,
NYSE=.N, NASDAQ=.O (or bare US ticker), LSE=.L, JSE=.J, HKEX=.HK. Chinese A-shares .SS/.SZ.

For each numbered operator, return the best RIC for its PRIMARY listing. If the company is private,
a subsidiary/JV with no listing, or you're not confident, set "ric": null with a short "note"
(e.g. "private", "JV", "uncertain").

Return ONLY a JSON object keyed by the operator's number, e.g.:
{"1": {"exchange": "ASX", "ric": "RMS.AX"}, "2": {"ric": null, "note": "private"}}

Operators:
%s
"""


# Corporate boilerplate (always ignored) vs. generic mining words (ignored only when keying on the
# DISTINCTIVE part). Splitting them fixes all-generic names like "Group 6 Metals" / "Mineral
# Resources", which have no distinctive token and would otherwise never match.
_CORP = {"inc", "corp", "corporation", "ltd", "limited", "plc", "nl", "sa", "spa", "ag", "the",
         "co", "company", "and", "of"}
_GENERIC = {"resources", "resource", "mining", "minerals", "mineral", "metals", "metal", "gold",
            "silver", "copper", "exploration", "explorations", "mines", "mine", "energy",
            "holdings", "group", "royalty", "royalties"}


def _tokens(name: str, *, strip_generic: bool = True) -> set[str]:
    stop = _CORP | (_GENERIC if strip_generic else set())
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
            if len(w) > 1 and w not in stop}


def _name_matches(operator: str, matched: str | None) -> bool:
    """True if the resolved company name is the operator. Keys on the distinctive token(s) when there
    are any ("Aldebaran"); for all-generic names ("Mineral Resources") requires every operator word
    to be present in the resolved name (so a lone shared "Gold"/"Copper" can't false-match)."""
    strong = _tokens(operator)
    matched_all = _tokens(matched or "", strip_generic=False)
    if strong:
        return bool(strong & matched_all)
    op_all = _tokens(operator, strip_generic=False)
    return bool(op_all) and op_all <= matched_all


@dataclass
class Resolution:
    operator: str
    proposed_ric: str | None = None
    proposed_note: str | None = None
    permid: str | None = None
    matched_name: str | None = None
    oldest_filing: str | None = None
    status: str = "unresolved"  # resolved_full | resolved_thin | resolved_no_filings | unresolved | not_listed


def propose_rics(operators: list[str]) -> dict[int, dict]:
    """Claude -> {index: {exchange?, ric|None, note?}} for each operator (1-indexed)."""
    listing = "\n".join(f"{i}. {name}" for i, name in enumerate(operators, 1))
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=_PROPOSE_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content": _PROMPT % listing}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    blob = re.search(r"\{.*\}", text, re.DOTALL)
    return {int(k): v for k, v in json.loads(blob.group(0) if blob else text).items()}


def symbology(cli: LSEG, ric: str) -> tuple[str | None, str | None]:
    """RIC -> (Organization PermID, Company Common Name), or (None, None)."""
    r = httpx.get(f"{SYMBOL_URL}?universe={httpx.QueryParams({'x': ric})['x']}",
                  headers={"Authorization": f"Bearer {cli.token}"}, timeout=30.0).json()
    row = (r.get("universe") or [{}])[0]
    return row.get("Organization PermID"), row.get("Company Common Name")


def oldest_filing(cli: LSEG, permid: str) -> tuple[str | None, int]:
    """(oldest FilingDate, count-on-first-page) across ALL feeds over ~35yr — the entity's depth."""
    query = (
        '{ FinancialFiling(filter: {AND: ['
        '{FilingDocument: {Identifiers: {OrganizationId: {EQ: "%s"}}}},'
        '{FilingDocument: {DocumentSummary: {FilingDate: {BETWN: {FROM: "1990-01-01T00:00:00Z", TO: "%s"}}}}}'
        ']}, sort: {FilingDocument: {DocumentSummary: {FilingDate: ASC}}}, limit: 1) {'
        ' FilingDocument { DocumentSummary { FilingDate } } } }'
    ) % (permid, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    rows = cli._gql(query).get("FinancialFiling") or []  # noqa: SLF001
    if not rows:
        return None, 0
    ds = (rows[0].get("FilingDocument") or {}).get("DocumentSummary") or {}
    fd = ds.get("FilingDate")
    return (fd[:10] if fd else None), 1


def resolve_operators(*, sample: int | None = None) -> list[Resolution]:
    """Run the full pipeline over the portfolio's distinct operators; write + return the manifest."""
    operators = sorted(portfolio.by_operator().keys())
    if sample:
        operators = operators[:sample]
    proposals = propose_rics(operators)

    cli = LSEG()
    thin_cutoff = dt.date.today().replace(year=dt.date.today().year - 3)  # <3yr history = suspicious
    out: list[Resolution] = []
    for i, name in enumerate(operators, 1):
        if i % 40 == 0:
            cli = LSEG()  # refresh the ~5-min token part-way through a long run
        r = Resolution(operator=name)
        p = proposals.get(i) or {}
        r.proposed_ric, r.proposed_note = p.get("ric"), p.get("note")
        try:
            if not r.proposed_ric:
                r.status = "not_listed"  # private / JV / unlisted — nothing to fetch
            else:
                r.permid, r.matched_name = symbology(cli, r.proposed_ric)
                if not r.permid:
                    r.status = "unresolved"  # Claude's RIC didn't resolve in LSEG
                elif not _name_matches(name, r.matched_name):
                    r.status = "resolved_mismatch"  # resolved to a DIFFERENT company — wrong ticker
                else:
                    r.oldest_filing, _ = oldest_filing(cli, r.permid)
                    if not r.oldest_filing:
                        r.status = "resolved_no_filings"
                    elif dt.date.fromisoformat(r.oldest_filing) > thin_cutoff:
                        r.status = "resolved_thin"  # entity quirk (e.g. Alamos) — 2nd look
                    else:
                        r.status = "resolved_full"
        except Exception as exc:  # noqa: BLE001 — one bad operator must not abort the 142-run
            r.status = f"error: {type(exc).__name__}"
        out.append(r)

    _OUT.parent.mkdir(exist_ok=True)
    _OUT.write_text(json.dumps([asdict(r) for r in out], indent=1), encoding="utf-8")
    return out

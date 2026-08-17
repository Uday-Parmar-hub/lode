"""Corpus recovery: re-attempt operators that failed single-guess resolution, using MULTIPLE
candidate RICs per operator (Claude proposes several; LSEG validates each — first that resolves AND
name-matches wins). Recovers the small juniors where one ticker guess missed. Same correctness
guarantee as resolve.py (a wrong candidate is rejected, never trusted). Updates the manifest in place.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import anthropic

from . import config
from .lseg import LSEG
from .resolve import _name_matches, oldest_filing, symbology

_RES = config.ROOT / "data" / "operator_resolution.json"
_MODEL = "claude-sonnet-4-6"
RECOVER_STATUSES = {"unresolved", "resolved_mismatch"}

_PROMPT = """For each numbered mining OPERATOR, give up to 4 candidate LSEG RICs (best guess first),
covering plausible exchange/ticker variants. Suffixes: TSX=.TO, TSXV=.V, ASX=.AX, NYSE=.N,
NASDAQ=.O (or bare US ticker), LSE=.L, JSE=.J, HKEX=.HK. If the company is genuinely private or
unlisted, return an empty list.

Return ONLY a JSON object keyed by the operator's number, e.g.:
{"1": ["ALDE.V", "ADN.TO"], "2": []}

Operators:
%s"""


def propose_candidates(operators: list[str]) -> dict[int, list[str]]:
    """Claude -> {index: [ric, ...]} — up to 4 candidate RICs per operator, best first."""
    listing = "\n".join(f"{i}. {n}" for i, n in enumerate(operators, 1))
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content": _PROMPT % listing}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    blob = re.search(r"\{.*\}", text, re.DOTALL)
    return {int(k): (v or []) for k, v in json.loads(blob.group(0) if blob else text).items()}


def recover() -> tuple[list[dict], int]:
    """Re-attempt the failed operators with multi-candidate RICs; update the manifest; return
    (all_rows, n_recovered)."""
    rows = json.loads(_RES.read_text(encoding="utf-8"))
    by_op = {r["operator"]: r for r in rows}
    failures = [r["operator"] for r in rows if r["status"] in RECOVER_STATUSES]
    if not failures:
        return rows, 0
    candidates = propose_candidates(failures)

    cli = LSEG()
    thin_cutoff = dt.date.today().replace(year=dt.date.today().year - 3)
    recovered = 0
    for i, name in enumerate(failures, 1):
        if i % 40 == 0:
            cli = LSEG()
        rec = by_op[name]
        for ric in candidates.get(i, []):
            try:
                permid, matched = symbology(cli, ric)
                if not (permid and _name_matches(name, matched)):
                    continue
                oldest, _ = oldest_filing(cli, permid)
                rec.update(proposed_ric=ric, permid=permid, matched_name=matched, oldest_filing=oldest)
                if not oldest:
                    rec["status"] = "resolved_no_filings"
                elif dt.date.fromisoformat(oldest) > thin_cutoff:
                    rec["status"] = "resolved_thin"
                else:
                    rec["status"] = "resolved_full"
                recovered += 1
                break
            except Exception:  # noqa: BLE001 — a bad candidate just moves to the next one
                continue

    _RES.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows, recovered

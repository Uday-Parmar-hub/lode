"""Manual identifier overrides for operators auto-resolution can't reach.

The long tail defeats name-guessing for real reasons: ticker reuse (GORO now belongs to Goldgroup),
CSE symbols the auto-guess misses (.CD suffix), US-only filers whose SEC ticker collides, and — the
common one here — 2026 RENAMES that post-date the resolver's knowledge, so the correct permid resolves
but under a new name the guard rejects (Taseko->Trekor, Minera Alamos->Mining Americas).

For an override the HUMAN asserts the identifier, so we trust it over the name-match: we take the first
RIC that resolves at all (recording the resolved name for audit) and/or accept a CIK verbatim. Status
becomes `resolved_manual`. Overrides live in data/manual_overrides.json (gitignored with the rest of the
portfolio-derived data); this file is the curation the human keeps adding to as the tail is worked.
"""
from __future__ import annotations

import json

from . import config
from .lseg import LSEG
from .resolve import _name_matches, oldest_filing, symbology

_RES = config.ROOT / "data" / "operator_resolution.json"
_OVR = config.ROOT / "data" / "manual_overrides.json"


def apply_overrides() -> tuple[list[dict], list[dict]]:
    """Patch operator_resolution.json from the overrides file; return (all_rows, applied_summaries)."""
    rows = json.loads(_RES.read_text(encoding="utf-8"))
    by_op = {r["operator"]: r for r in rows}
    overrides = json.loads(_OVR.read_text(encoding="utf-8"))

    cli = LSEG()
    applied: list[dict] = []
    for op, ov in overrides.items():
        rec = by_op.get(op)
        if rec is None:  # operator not in the manifest (e.g. newly-added portfolio name)
            rec = {"operator": op}
            rows.append(rec)
            by_op[op] = rec

        permid = matched = chosen = None
        for ric in ov.get("rics", []):
            try:
                pid, name = symbology(cli, ric)
            except Exception:  # noqa: BLE001 — a bad candidate just moves to the next
                continue
            if pid:
                permid, matched, chosen = pid, name, ric
                if _name_matches(op, name):
                    break  # a name-matching hit is ideal; otherwise keep the (trusted) resolved one

        cik = ov.get("cik")
        if cik:
            cik = f"{int(cik):010d}"
        oldest = None
        if permid:
            try:
                oldest, _ = oldest_filing(cli, permid)
            except Exception:  # noqa: BLE001
                oldest = None

        rec.update(proposed_ric=chosen, permid=permid, matched_name=matched, oldest_filing=oldest,
                   cik_override=cik, status="resolved_manual", override_note=ov.get("note"))
        applied.append({"operator": op, "ric": chosen, "permid": permid, "cik": cik,
                        "matched_name": matched,
                        "name_matched": _name_matches(op, matched) if matched else None})

    _RES.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows, applied

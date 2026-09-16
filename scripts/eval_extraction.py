"""Extraction eval — the quality gate for the LLM extraction step.

Runs royalty.extract over the labeled PR fixtures (tests/fixtures/royalty_prs.json) and scores it:
precision/recall of the royalties found, holder accuracy on matched royalties, and whether negative
cases (no royalty) are correctly left empty. Deterministic (temperature=0), so a prompt or model change
can be *graded* against a baseline instead of eyeballed.

    python scripts/eval_extraction.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import royalty  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "royalty_prs.json"


def _num(s: str | None) -> float | None:
    m = re.search(r"[\d.]+", s or "")
    return float(m.group()) if m else None


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _holder_ok(expected: str | None, got: str | None) -> bool:
    e = _norm(expected)
    if not e:  # label left the holder unspecified -> don't penalize
        return True
    g = _norm(got)
    return bool(g) and (e in g or g in e)


def _match(exp: dict, gots: list, used: set) -> object | None:
    """A got royalty matching this expected one on type + rate (rate within 0.05), not already used."""
    et, er = _norm(exp.get("royalty_type")), _num(exp.get("rate"))
    for g in gots:
        if id(g) in used:
            continue
        gt, gr = _norm(g.royalty_type), _num(g.rate)
        type_ok = bool(et and gt) and (et in gt or gt in et)
        rate_ok = er is not None and gr is not None and abs(er - gr) < 0.05
        if type_ok and rate_ok:
            return g
    return None


def main() -> None:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    tp = fp = fn = 0
    holder_ok = holder_tot = 0
    neg_ok = neg_tot = 0

    print("=" * 74)
    print(f"EXTRACTION EVAL  ({len(fixtures)} fixtures)")
    print("=" * 74)

    for fx in fixtures:
        exp = fx["expected"]
        passages = royalty.royalty_passages(fx["text"])
        gots = royalty.extract(passages, operator_hint=None).royalties if passages else []

        if not exp:  # negative case: correct iff nothing extracted
            neg_tot += 1
            ok = len(gots) == 0
            neg_ok += ok
            fp += len(gots)
            print(f"\n[{'PASS' if ok else 'FAIL'}] {fx['name']} (negative) — extracted {len(gots)}, expected 0")
            continue

        used: set = set()
        print(f"\n[{fx['name']}]  expected {len(exp)}, extracted {len(gots)}")
        for e in exp:
            g = _match(e, gots, used)
            if g:
                used.add(id(g))
                tp += 1
                hok = _holder_ok(e.get("holder"), g.holder)
                holder_tot += 1
                holder_ok += hok
                tail = "holder ok" if hok else f"holder MISS (got: {g.holder})"
                print(f"    ok  {e['royalty_type']} {e['rate']} / {e.get('holder')}  [{tail}]")
            else:
                fn += 1
                print(f"    XX  MISSED: {e['royalty_type']} {e['rate']} / {e.get('holder')}")
        extra = len(gots) - len(used)
        if extra > 0:
            fp += extra
            print(f"    +{extra} extra royalt{'y' if extra == 1 else 'ies'} extracted (not in the label)")

    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    print("\n" + "=" * 74)
    print(f"Royalties   TP={tp}  FP={fp}  FN={fn}   ->   precision {prec:.0%}   recall {rec:.0%}")
    if holder_tot:
        print(f"Holder accuracy (on matched royalties): {holder_ok}/{holder_tot} = {holder_ok / holder_tot:.0%}")
    print(f"Negatives (correctly found no royalty): {neg_ok}/{neg_tot}")
    print("=" * 74)


if __name__ == "__main__":
    main()

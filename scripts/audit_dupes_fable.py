"""Independent duplicate audit with a SECOND model (Fable 5) — Matt's "different agent" cross-check.

    python scripts/audit_dupes_fable.py

Read-only. Touches NOTHING in the DB. It asks claude-fable-5 to independently second-guess our dedup in
BOTH directions and writes a detailed report to data/fable_audit.json + a console summary:

  PASS A — MISSED MERGES (under-merge): pairs of DISTINCT instruments that look like the SAME royalty
           (same asset+type+rate, separate instrument_ids — a genuine royalty STACK, or a merge our
           holder-resolver left apart). Fable judges same-vs-distinct.
  PASS B — WRONG MERGES (over-merge): instruments our dedup collapsed from several source records whose
           members disagree (different rate/type, or unrelated holders). Fable judges whether they were
           correctly one royalty or distinct royalties wrongly fused (lost signal).

Fable is deliberately a DIFFERENT model from the Opus-5 resolvers that did the dedup — a second, independent
pass catches errors the first model is blind to. Findings are flagged for human review; nothing is applied.
"""
from __future__ import annotations

import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import anthropic  # noqa: E402

from techreport import config, db  # noqa: E402

MODEL = "claude-fable-5"
LEDGER = config.ROOT / "data" / "fable_audit.json"
WORKERS = 6

_ACC = "'áàâäãéèêëíìîïóòôöõúùûüçñ','aaaaaeeeeiiiiooooouuuucn'"
_NA = (rf"regexp_replace(translate(regexp_replace(lower(project_name),'\(.*?\)','','g'),{_ACC}),'[^a-z0-9]+','','g')")
_NH = (rf"regexp_replace(translate(regexp_replace(lower(coalesce(holder,'')),'\(.*?\)','','g'),{_ACC}),'[^a-z0-9]+','','g')")
_CT = ("case when lower(coalesce(royalty_type,''))~'stream' then 'STREAM' "
       "when lower(coalesce(royalty_type,''))~'nsr|net smelter' then 'NSR' "
       "when lower(coalesce(royalty_type,''))~'npi|net prof|net proc' then 'NPI' "
       "when lower(coalesce(royalty_type,''))~'gross|gor|gsr|overrid|gvr' then 'GROSS' "
       "when lower(coalesce(royalty_type,''))~'advance|amr' then 'AMR' else lower(coalesce(royalty_type,'?')) end")
_RK = r"coalesce(rate_pct::text,regexp_replace(lower(coalesce(rate,'')),'[^a-z0-9.]','','g'))"

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _judge(system: str, user: str, props: dict) -> dict:
    tool = {"name": "judge", "description": "Return the verdict.",
            "input_schema": {"type": "object", "properties": props, "required": list(props)}}
    msg = _client.messages.create(
        model=MODEL, max_tokens=700, system=system, tools=[tool],
        tool_choice={"type": "tool", "name": "judge"},
        messages=[{"role": "user", "content": user}])
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    return {}


# ── PASS A — missed merges ────────────────────────────────────────────────────
SYS_A = """You audit a mining royalty-ORIGINATION database for DUPLICATES a dedup step may have missed.
You get TWO royalty entries that sit on the same/similar asset with the same type and rate but are stored
as SEPARATE instruments. Decide whether they are the SAME underlying royalty (one real encumbrance that
should be ONE row) or genuinely DISTINCT royalties (a "stack" — several separate royalties that merely
share a rate, held by unrelated parties). Same entity under name variants / parent-subsidiary / a royalty
conveyed between parties over time = SAME. Different unrelated holders each with their own royalty = DISTINCT.
When truly unsure, say DISTINCT (merging two real royalties loses signal). Judge independently."""

PROPS_A = {"verdict": {"type": "string", "enum": ["same", "distinct"]},
           "confidence": {"type": "integer", "description": "1=guess, 5=certain"},
           "reason": {"type": "string"}}


def _fmt(e: dict) -> str:
    return (f"asset={e['project_name']!r} | operator={e['operator']!r} | juris={e['jurisdiction']!r} | "
            f"type={e['royalty_type']!r} | rate={e['rate']!r} | holder={e['holder']!r} | "
            f"holder_note={e['holder_note']!r} | sources={e['sources']}")


def pass_a() -> list[dict]:
    with db.connect() as c:
        cur = c.cursor(); cur.execute("set pg_trgm.similarity_threshold=0.3")
        cur.execute(f"""
          with p as (select instrument_id, {_NA} a, {_CT} t, {_RK} r, {_NH} h from royalties where is_primary)
          select p1.instrument_id, p2.instrument_id from p p1 join p p2 on p1.instrument_id<p2.instrument_id
          where p1.t=p2.t and (
            (p1.a=p2.a and p1.r=p2.r) or
            (similarity(p1.a,p2.a)>0.35 and p1.r=p2.r and p1.h=p2.h and p1.h<>''))
        """)
        pairs = cur.fetchall()
        ids = sorted({x for pr in pairs for x in pr})
        cur.execute("""
          select instrument_id, project_name, operator, jurisdiction, royalty_type, rate, holder, holder_note,
                 (select string_agg(distinct source_label, '; ') from royalties d where d.instrument_id=r.instrument_id) sources
          from royalties r where is_primary and instrument_id = any(%s)""", (ids,))
        ctx = {row[0]: dict(instrument_id=row[0], project_name=row[1], operator=row[2], jurisdiction=row[3],
                            royalty_type=row[4], rate=row[5], holder=row[6], holder_note=row[7], sources=row[8]) for row in cur.fetchall()}
    print(f"PASS A — {len(pairs)} candidate pairs -> {MODEL}")

    def run(pr):
        a, b = ctx[pr[0]], ctx[pr[1]]
        v = _judge(SYS_A, f"Entry 1: {_fmt(a)}\nEntry 2: {_fmt(b)}\n\nSame royalty or distinct?", PROPS_A)
        return {"pass": "A", "kind": "missed_merge_candidate", "a": a, "b": b, **v}

    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(run, pr) for pr in pairs]
        for i, f in enumerate(as_completed(futs), 1):
            out.append(f.result())
            if i % 25 == 0: print(f"  ...{i}/{len(pairs)}")
    return out


# ── PASS B — wrong merges ─────────────────────────────────────────────────────
SYS_B = """You audit a mining royalty database for OVER-merging. An automated step fused several source
records into ONE royalty ("instrument"). You get the member records (holder / rate / type, per source
report). Decide: were they CORRECTLY one royalty (same encumbrance under name variants, parent-subsidiary,
or a holder that changed over time), or were DISTINCT royalties WRONGLY merged (e.g. unrelated holders, or
different rates/types that indicate separate encumbrances)? If wrongly merged, say which members should be
split out. When unsure, lean 'wrong_merge' only if the members are clearly unrelated — otherwise 'correct'."""

PROPS_B = {"verdict": {"type": "string", "enum": ["correct", "wrong_merge"]},
           "confidence": {"type": "integer", "description": "1=guess, 5=certain"},
           "reason": {"type": "string"},
           "split_out": {"type": "string", "description": "which members look distinct, or '' if none"}}


def pass_b() -> list[dict]:
    with db.connect() as c:
        cur = c.cursor()
        cur.execute(f"""
          with v as (select distinct on (instrument_id, source_docid) instrument_id, {_NH} h, {_RK} r, {_CT} t
                     from royalties order by instrument_id, source_docid)
          select instrument_id from (select instrument_id, count(distinct h) nh, count(distinct r) nr, count(distinct t) nt
                 from v group by instrument_id having count(*)>1) x
          where nr>1 or nt>1 or nh>1""")
        ids = [r[0] for r in cur.fetchall()]
        cur.execute("""
          select instrument_id, project_name, string_agg(distinct
                 coalesce(royalty_type,'?')||' | '||coalesce(rate,'?')||' | '||coalesce(holder,'∅')||
                 '  ('||coalesce(source_label,'?')||')', E'\n   ') members
          from royalties where instrument_id = any(%s) group by instrument_id, project_name""", (ids,))
        grp = [{"instrument_id": r[0], "project_name": r[1], "members": r[2]} for r in cur.fetchall()]
    print(f"PASS B — {len(grp)} merged instruments to audit -> {MODEL}")

    def run(g):
        v = _judge(SYS_B, f"Asset: {g['project_name']!r}\nMerged members (holder | rate | type):\n   {g['members']}\n\nCorrectly one royalty, or wrongly merged?", PROPS_B)
        return {"pass": "B", "kind": "wrong_merge_candidate", "instrument_id": g["instrument_id"],
                "project_name": g["project_name"], "members": g["members"], **v}

    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(run, g) for g in grp]
        for i, f in enumerate(as_completed(futs), 1):
            out.append(f.result())
            if i % 25 == 0: print(f"  ...{i}/{len(grp)}")
    return out


def _write(findings: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(findings, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> None:
    a = pass_a()
    _write(a)                 # persist Pass A immediately so a later error can't discard it
    findings = a + pass_b()
    _write(findings)

    missed = [f for f in findings if f["pass"] == "A" and f.get("verdict") == "same"]
    wrong = [f for f in findings if f["pass"] == "B" and f.get("verdict") == "wrong_merge"]
    hi = lambda xs: [x for x in xs if (x.get("confidence") or 0) >= 4]
    print("\n" + "=" * 78)
    print(f"FABLE-5 DUPLICATE AUDIT — {len(findings)} candidates judged. Full detail: {LEDGER}")
    print(f"  MISSED MERGES (under-merge): {len(missed)} flagged same  ({len(hi(missed))} high-confidence)")
    print(f"  WRONG MERGES  (over-merge):  {len(wrong)} flagged  ({len(hi(wrong))} high-confidence)")
    print("=" * 78)
    print("\n--- MISSED MERGES (should likely be ONE royalty), high-confidence first ---")
    for f in sorted(missed, key=lambda x: -(x.get("confidence") or 0))[:25]:
        print(f"  [{f.get('confidence')}] {f['a']['project_name']}  ⟷  {f['b']['project_name']}")
        print(f"       {f['a']['holder']!r} / {f['a']['rate']}  vs  {f['b']['holder']!r} / {f['b']['rate']}")
        print(f"       → {f.get('reason','')[:150]}")
    print("\n--- WRONG MERGES (distinct royalties possibly fused), high-confidence first ---")
    for f in sorted(wrong, key=lambda x: -(x.get("confidence") or 0))[:25]:
        print(f"  [{f.get('confidence')}] {f['project_name']}  → split: {f.get('split_out','')[:80]}")
        print(f"       → {f.get('reason','')[:150]}")


if __name__ == "__main__":
    main()

"""Extract royalties from ONE MarketWatch PR and insert them into a LODE database.

Backs the dashboard "Add to LODE" button. Reads a JSON object (argv[1] = a file path, or stdin) —
{docid, company, date, url, text} — extracts royalties with royalty.extract, inserts each into
`royalties` (ingested_from='marketwatch', origin='marketwatch', status='pending') in the DATABASE_URL
DB, links them into the memory chain (dup_key / instrument_id / is_primary, via dedupe.py's canonical
key), and prints a JSON result. `text` must be the PRESS RELEASE body, not a summary of it — the
quote_verified check is made against whatever text is passed in. Point DATABASE_URL at lode_test for
the local demo; nothing else is touched.

    DATABASE_URL=postgresql://lode:lode@localhost:5433/lode_test \
      python scripts/add_one_to_lode.py story.json
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import db, royalty  # noqa: E402

NAME2SYM = {"gold": "Au", "silver": "Ag", "copper": "Cu", "molybdenum": "Mo", "moly": "Mo",
            "nickel": "Ni", "zinc": "Zn", "lead": "Pb", "cobalt": "Co", "uranium": "U",
            "platinum": "PGE", "palladium": "PGE", "pge": "PGE", "pgm": "PGE", "iron": "Fe",
            "vanadium": "V", "lithium": "Li", "tin": "Sn", "tungsten": "W", "graphite": "C"}


def commodities(s: str | None) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"[,/&]|\band\b", (s or "")):
        t = tok.strip()
        if not t:
            continue
        sym = NAME2SYM.get(t.lower())
        if sym:
            out.append(sym)
        elif 1 <= len(t) <= 4 and t[0].isupper():
            out.append(t)
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def rate_pct(s: str | None) -> float | None:
    if not s or "%" not in s:
        return None
    m = re.search(r"[\d.]+", s)
    if not m:
        return None
    v = float(m.group())
    return v if v <= 25 else None


INSERT = """
INSERT INTO royalties
 (project_name, operator, commodity, jurisdiction, stage,
  royalty_type, rate, rate_pct, holder, royalty_available,
  partial_coverage, advance_payments, production_threshold, production_cap, buyback, step_down, rofr, features_note,
  regime, source_docid, source_label, source_url, source_date, source_quote, quote_verified,
  status, ingested_from, origin, is_primary)
 VALUES (%(project_name)s,%(operator)s,%(commodity)s,%(jurisdiction)s,%(stage)s,
  %(royalty_type)s,%(rate)s,%(rate_pct)s,%(holder)s,'unknown',
  %(partial_coverage)s,%(advance_payments)s,%(production_threshold)s,%(production_cap)s,%(buyback)s,%(step_down)s,%(rofr)s,%(features_note)s,
  %(regime)s,%(source_docid)s,%(source_label)s,%(source_url)s,%(source_date)s,%(source_quote)s,%(quote_verified)s,
  'pending','marketwatch','marketwatch',true)
 ON CONFLICT (source_docid, project_name, holder, royalty_type) DO NOTHING
"""


def collapse_repeats(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop royalties repeated verbatim within one extraction. Returns (kept, dropped_count).

    The unique index compares NULLs as DISTINCT, so ON CONFLICT never fires when `holder` is NULL —
    which is how the corpus accumulated the same royalty up to 14 times from a single document. This
    compares the tuple directly, so a NULL holder still matches a NULL holder.
    """
    seen: set[tuple] = set()
    kept: list[dict] = []
    for r in rows:
        key = (r["project_name"], r["holder"], r["royalty_type"], r["rate"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(r)
    return kept, len(rows) - len(kept)


def _dedupe_module():
    """Load scripts/dedupe.py as a module so we reuse its canonical dup-key SQL rather than
    re-implementing it here (main() is __main__-guarded, so importing has no side effects).
    One definition of "same real-world royalty" — if dedupe.py's key changes, this follows."""
    path = pathlib.Path(__file__).resolve().parent / "dedupe.py"
    spec = importlib.util.spec_from_file_location("dedupe", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _link_into_memory_chain(cur, docid: str) -> dict:
    """Give the just-inserted rows their dup_key / instrument_id / is_primary, using the SAME canonical
    key as scripts/dedupe.py, scoped to the groups this PR touches.

    Without this a bridged royalty arrives with a NULL instrument_id, and the dashboard's edit path
    (saveFactEdit) demotes the old version with `WHERE instrument_id = <null>` — which matches nothing,
    so the analyst's first correction leaves TWO is_primary rows for one royalty. Linking here also
    collapses the PR into an existing technical-report instrument when LODE already has that royalty,
    which is the cross-source corroboration the bridge exists to produce.

    A full `python scripts/dedupe.py` run stays the authority (it also applies the semantic ledgers);
    this is the same deterministic assignment, done for one PR at insert time.
    """
    dd = _dedupe_module()
    # load_asset_aliases creates a temp table without IF NOT EXISTS, so guard it: this function may be
    # called more than once inside one transaction (two releases linked together, or a test).
    cur.execute("select to_regclass('asset_alias') is not null")
    if not cur.fetchone()[0]:
        dd.load_asset_aliases(cur)  # + the asset-rename ledger if present; no-op without one

    cur.execute(f"update royalties set dup_key = {dd.DUPKEY_SQL} where source_docid = %s", (docid,))
    cur.execute("select distinct dup_key from royalties where source_docid = %s and dup_key is not null",
                (docid,))
    keys = [r[0] for r in cur.fetchall()]
    if not keys:
        return {"instruments": 0, "joined_existing": 0, "flagged_revalidation": 0}

    # How many of these groups already existed (i.e. this PR corroborates a royalty LODE already had)?
    cur.execute("""select count(*) from (
                     select dup_key from royalties
                      where dup_key = any(%s) and source_docid <> %s
                      group by dup_key) g""", (keys, docid))
    joined = cur.fetchone()[0]

    # instrument_id: REUSE the group's existing id where there is one, mint only for a new instrument.
    # Mirrors dedupe.py. Restricted to rows that have none, so established rows are never rewritten.
    cur.execute("""
        with grp as (
          select dup_key,
                 coalesce(max(instrument_id) filter (where instrument_id is not null),
                          'inst_'||substr(md5(dup_key||clock_timestamp()::text||random()::text),1,20)) as iid
            from royalties where dup_key = any(%s) group by dup_key
        )
        update royalties r set instrument_id = grp.iid
          from grp where r.dup_key = grp.dup_key and r.instrument_id is null
    """, (keys,))

    # is_primary within the affected groups only — same ranking dedupe.py uses.
    cur.execute("""
        with ranked as (
          select id, row_number() over (
                   partition by dup_key
                   order by source_date desc nulls last, quote_verified desc,
                            extract_confidence desc nulls last, id desc) as rn
            from royalties where dup_key = any(%s)
        )
        update royalties r set is_primary = (ranked.rn = 1) from ranked where ranked.id = r.id
    """, (keys,))

    # A new source landing on an already-validated instrument must go back for re-review
    # (migration 003's contract). Flag the surfaced row, as apply_audit_fixes.py does.
    cur.execute("""
        update royalties r set needs_revalidation = true
         where r.dup_key = any(%s) and r.is_primary
           and exists (select 1 from royalties v
                        where v.dup_key = r.dup_key and v.status = 'validated')
    """, (keys,))
    flagged = cur.rowcount

    return {"instruments": len(keys), "joined_existing": joined, "flagged_revalidation": flagged}


def main() -> None:
    raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else sys.stdin.read()
    rec = json.loads(raw)
    docid = rec.get("docid")

    # Idempotent per press release: one PR -> one ingestion. If this story is already in LODE, don't
    # re-extract (saves the Claude call) and don't duplicate — just report it's already there.
    if docid:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT project_name FROM royalties WHERE source_docid = %s LIMIT 1", (docid,))
                row = cur.fetchone()
        if row:
            print(json.dumps({"inserted": 0, "already": True, "project": row[0]}))
            return

    text = rec.get("text") or ""
    ntext = re.sub(r"\s+", " ", text).strip().lower()

    passages = royalty.royalty_passages(text)
    if not passages:
        print(json.dumps({"inserted": 0, "reason": "no royalty passage found in this release"}))
        return

    ex = royalty.extract(passages, operator_hint=rec.get("company") or None)
    rows = []
    for r in ex.royalties:
        rows.append({
            "project_name": ex.project_name or rec.get("company") or "?",
            "operator": rec.get("company"),
            "commodity": commodities(ex.commodity),
            "jurisdiction": ex.jurisdiction,
            "stage": ex.stage,
            "royalty_type": r.royalty_type,
            "rate": r.rate,
            "rate_pct": rate_pct(r.rate),
            "holder": r.holder,
            "partial_coverage": r.partial_coverage,
            "advance_payments": r.advance_payments,
            "production_threshold": r.production_threshold,
            "production_cap": r.production_cap,
            "buyback": r.buyback,
            "step_down": r.step_down,
            "rofr": r.rofr,
            "features_note": getattr(r, "other_terms", None),
            "regime": "MarketWatch",
            "source_docid": rec.get("docid"),
            "source_label": "MarketWatch · " + (rec.get("date") or ""),
            "source_url": rec.get("url"),
            "source_date": rec.get("date"),
            "source_quote": re.sub(r"</?b>", "", r.quote or ""),
            "quote_verified": bool(r.quote) and re.sub(r"\s+", " ", (r.quote or "")).strip().lower()[:80] in ntext,
        })

    extracted = len(rows)
    rows, repeats = collapse_repeats(rows)

    stored: list[dict] = []
    skipped: list[dict] = []
    chain = {"instruments": 0, "joined_existing": 0, "flagged_revalidation": 0}
    if rows:
        with db.connect() as conn:
            with conn.cursor() as cur:
                # One row at a time so cur.rowcount tells us what was actually STORED. executemany
                # reports only the batch, and ON CONFLICT DO NOTHING drops silently: the unique index is
                # (source_docid, project_name, holder, royalty_type) and the RATE is not in it, so two
                # genuinely different royalties from one release that share project+holder+type (a 2%
                # and a 1% NSR both held by "the vendors") collide and the second is thrown away.
                # Reporting len(rows) called that a success, and because the docid probe above then
                # treats the release as done, the lost royalty could never be added again.
                for r in rows:
                    cur.execute(INSERT, r)
                    (stored if cur.rowcount == 1 else skipped).append(r)
                if stored and docid:
                    chain = _link_into_memory_chain(cur, docid)
            conn.commit()

    out = {
        "inserted": len(stored),
        "extracted": extracted,
        "project": ex.project_name,
        "royalties": [{"type": r.royalty_type, "rate": r.rate, "holder": r.holder} for r in ex.royalties],
        **chain,
    }
    if repeats:
        out["repeats_collapsed"] = repeats   # identical royalties stated twice in one release
    if skipped:
        out["skipped"] = len(skipped)
        out["skipped_detail"] = [
            {"type": r["royalty_type"], "rate": r["rate"], "holder": r["holder"]} for r in skipped
        ]
    print(json.dumps(out))


if __name__ == "__main__":
    main()

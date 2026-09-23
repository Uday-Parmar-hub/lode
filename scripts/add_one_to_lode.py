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

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import chain, db, royalty  # noqa: E402
from techreport.commodity import commodities  # noqa: E402


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
 (project_name, operator, commodity, jurisdiction, stage, is_producing,
  royalty_type, rate, rate_pct, holder, royalty_available,
  partial_coverage, advance_payments, production_threshold, production_cap, buyback, step_down, rofr, features_note,
  regime, source_docid, source_label, source_url, source_date, source_quote, quote_verified,
  status, ingested_from, origin, is_primary)
 VALUES (%(project_name)s,%(operator)s,%(commodity)s,%(jurisdiction)s,%(stage)s,%(is_producing)s,
  %(royalty_type)s,%(rate)s,%(rate_pct)s,%(holder)s,'unknown',
  %(partial_coverage)s,%(advance_payments)s,%(production_threshold)s,%(production_cap)s,%(buyback)s,%(step_down)s,%(rofr)s,%(features_note)s,
  %(regime)s,%(source_docid)s,%(source_label)s,%(source_url)s,%(source_date)s,%(source_quote)s,%(quote_verified)s,
  'pending','marketwatch','marketwatch',true)
 ON CONFLICT (source_docid, project_name, holder, royalty_type) DO NOTHING
"""


# Every field that distinguishes one royalty from another on the write path. Keyed on all of them so
# collapsing is genuinely lossless: on (project, holder, type, rate) alone, two royalties that agree on
# the rate but differ in their TERMS — one capped, one not; one with a buyback, one without — would look
# like duplicates and the second would be silently dropped. Those are different instruments.
_REPEAT_KEY_FIELDS = (
    "project_name", "holder", "royalty_type", "rate",
    "partial_coverage", "advance_payments", "production_threshold", "production_cap",
    "buyback", "step_down", "rofr", "features_note",
)


def collapse_repeats(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop royalties repeated verbatim within one extraction. Returns (kept, dropped_count).

    The unique index compares NULLs as DISTINCT, so ON CONFLICT never fires when `holder` is NULL —
    which is how the corpus accumulated the same royalty up to 14 times from a single document. This
    compares the tuples directly, so a NULL field still matches a NULL field.
    """
    seen: set[tuple] = set()
    kept: list[dict] = []
    for r in rows:
        key = tuple(r.get(f) for f in _REPEAT_KEY_FIELDS)
        if key in seen:
            continue
        seen.add(key)
        kept.append(r)
    return kept, len(rows) - len(kept)


def _with_extractor_note(other_terms: str | None, notes: str | None) -> str | None:
    """Keep the extractor's own caveats on the row that lands in the review queue.

    `notes` is the field whose whole purpose is "anything ambiguous a human should check" — e.g. that
    a royalty is held by the ISSUER rather than a third party, or that a government royalty was seen
    and excluded. It is extraction-level, so it is appended to each royalty from that release rather
    than dropped; the batch path keeps it in its reviewable ledger, this path had nowhere else to put
    it and threw it away.
    """
    if not notes:
        return other_terms
    flat = re.sub(r"\s+", " ", notes).strip()[:400]
    note = f"[extractor note] {flat}"
    return f"{other_terms}\n\n{note}" if other_terms else note


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

    # issuer_hint, NOT operator_hint: the wire item tells us who ISSUED the release, not who
    # operates the property, and asserting the issuer is the operator excludes a royalty the
    # issuer itself holds — which is exactly the kind this tool exists to find.
    ex = royalty.extract(passages, issuer_hint=rec.get("company") or None)
    rows = []
    for r in ex.royalties:
        rows.append({
            "project_name": ex.project_name or rec.get("company") or "?",
            # The operator the extractor READ FROM THE TEXT, not the release's issuer. Stamping the
            # issuer here was affirmatively wrong in the case this tool most cares about: on an
            # Orogen Royalties release the row claimed Orogen operated First Majestic's Ermitaño
            # mine, while the extraction itself had correctly identified both parties. NULL when the
            # text does not say — better unknown than wrong, and operator is not part of dup_key.
            "operator": ex.operator,
            "commodity": commodities(ex.commodity),
            "jurisdiction": ex.jurisdiction,
            "stage": ex.stage,
            "is_producing": ex.is_producing,
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
            "features_note": _with_extractor_note(getattr(r, "other_terms", None), ex.notes),
            "regime": "MarketWatch",
            "source_docid": rec.get("docid"),
            # Records WHO staged this release, not just when — the bridge is a human-gated action and
            # nothing else in either database captured the actor.
            "source_label": " · ".join(
                x for x in ("MarketWatch", rec.get("date") or None, rec.get("actor") or None) if x),
            "source_url": rec.get("url"),
            "source_date": rec.get("date"),
            "source_quote": re.sub(r"</?b>", "", r.quote or ""),
            "quote_verified": bool(r.quote) and re.sub(r"\s+", " ", (r.quote or "")).strip().lower()[:80] in ntext,
        })

    extracted = len(rows)
    rows, repeats = collapse_repeats(rows)

    stored: list[dict] = []
    skipped: list[dict] = []
    linked = {"instruments": 0, "joined_existing": 0, "flagged_revalidation": 0}
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
                            # dup_key / instrument_id / is_primary, by the same definitions dedupe.py uses.
                    linked = chain.link(cur, "source_docid = %s", (docid,))
            conn.commit()

    out = {
        "inserted": len(stored),
        "extracted": extracted,
        "project": ex.project_name,
        "royalties": [{"type": r.royalty_type, "rate": r.rate, "holder": r.holder} for r in ex.royalties],
        **linked,
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

"""DRY-RUN PROOF — MarketWatch -> LODE integration (Phase 1). READ-ONLY. Writes NOTHING, anywhere.

Answers one question, with a number: of the royalty-change events MarketWatch has already caught, how
many are on properties LODE does not yet track? That is the live origination signal LODE's technical-
report feed is structurally missing (technical reports lag; press releases don't).

This is a DRY RUN: no Claude calls, no re-extraction (that's Phase 2), and no database writes. Any DB
connection is opened read-only and the script only ever issues SELECT, so it cannot touch prod.

INPUTS
  LODE side (what we already have): the on-disk extraction `data/royalty_pilot.json` (default), or a
      LODE DB via --lode-db (read-only). These are the properties LODE already covers.
  MW side (the news feed): --mw-db "<dsn>" runs one READ-ONLY SELECT of `royalty_change` stories, or
      --mw-file <export.json> for a fully offline run.

RUN FOR REAL NUMBERS (Azure Cloud Shell — 5432 is blocked off-corp):
  python dryrun_marketwatch_lode.py --mw-db "$MW_DATABASE_URL"

The MW DSN is MarketWatch's read connection (or-mw-pg01 / prm). Use a read-only role if you have one;
the script also forces the connection read-only regardless.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LODE_PILOT = ROOT / "data" / "royalty_pilot.json"

# Words that don't distinguish a property (so "Casino Project" and "Casino" match). Kept conservative:
# we strip generic descriptors, never a real name token.
_STOP = {
    "project", "projects", "mine", "mines", "property", "properties", "deposit", "deposits",
    "prospect", "prospects", "claim", "claims", "the", "gold", "silver", "copper", "zinc",
}
_MW_ROYALTY_TIER = "royalty_change"


def _norm(name: str | None) -> str:
    """Normalize a property name for matching: lowercase, alnum tokens, drop generic descriptors."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [t for t in toks if t not in _STOP]
    return " ".join(core or toks)  # if stripping emptied it, fall back to the raw tokens


@dataclass
class MWStory:
    """A MarketWatch royalty_change story, reduced to what the match needs."""

    asset_name: str | None
    company_name: str | None
    commodity: str | None
    claimed_exposure: str | None
    angle: str | None
    published_at: str | None


def load_lode_properties(path: Path) -> dict[str, str]:
    """Normalized project name -> a representative original, from the LODE extraction on disk."""
    data = json.loads(path.read_text(encoding="utf-8"))
    props: dict[str, str] = {}
    for row in data:
        pn = (row.get("project_name") or "").strip()
        if pn:
            props.setdefault(_norm(pn), pn)
    return props


def best_match(name: str, index: dict[str, str], threshold: float = 0.88) -> str | None:
    """Return the LODE property this MW asset name maps to, or None. Property-level, deliberately
    conservative: exact normalized hit, token-subset, or a high fuzzy ratio."""
    key = _norm(name)
    if not key:
        return None
    if key in index:
        return index[key]
    ktoks = set(key.split())
    for norm_key, original in index.items():
        ptoks = set(norm_key.split())
        if ktoks and ptoks and (ktoks <= ptoks or ptoks <= ktoks):
            return original
        if SequenceMatcher(None, key, norm_key).ratio() >= threshold:
            return original
    return None


def load_mw_from_db(dsn: str, min_materiality: int) -> list[MWStory]:
    """One READ-ONLY SELECT of MarketWatch royalty_change stories. No writes are possible."""
    import psycopg  # imported lazily so the offline path needs no driver

    # DISTINCT ON (document_id) + is_duplicate=false collapses the same event across prompt versions
    # and within-version dedup groups, so each royalty-change document is counted once.
    query = """
        SELECT DISTINCT ON (s.document_id)
               a.canonical_name AS asset_name,
               c.canonical_name AS company_name,
               s.core_commodity AS commodity,
               s.claimed_exposure,
               s.angle,
               s.published_at::text AS published_at
          FROM stories s
          LEFT JOIN assets    a ON a.sp_asset_id   = s.sp_asset_id
          LEFT JOIN companies c ON c.sp_company_id = s.sp_company_id
         WHERE s.tier = %s
           AND s.is_duplicate = false
           AND s.materiality >= %s
         ORDER BY s.document_id, s.published_at DESC
    """
    with psycopg.connect(dsn) as conn:
        conn.read_only = True  # every transaction on this connection is READ ONLY — no writes
        with conn.cursor() as cur:
            cur.execute(query, (_MW_ROYALTY_TIER, min_materiality))
            cols = [d.name for d in cur.description]
            return [MWStory(**dict(zip(cols, r))) for r in cur.fetchall()]


def load_mw_from_file(path: Path) -> list[MWStory]:
    """Offline: a JSON array of royalty_change stories (same fields the DB query returns)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        MWStory(
            asset_name=r.get("asset_name"),
            company_name=r.get("company_name"),
            commodity=r.get("commodity"),
            claimed_exposure=r.get("claimed_exposure"),
            angle=r.get("angle"),
            published_at=r.get("published_at"),
        )
        for r in data
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dry-run: what would MarketWatch add to LODE? (read-only)")
    ap.add_argument("--mw-db", help="MarketWatch DSN — one READ-ONLY SELECT (run in Cloud Shell).")
    ap.add_argument("--mw-file", help="Offline JSON export of royalty_change stories.")
    ap.add_argument("--lode-file", default=str(LODE_PILOT), help="LODE extraction on disk.")
    ap.add_argument("--min-materiality", type=int, default=0, help="MW materiality floor (0-10); 0 = all.")
    ap.add_argument("--sample", type=int, default=15, help="How many 'new to LODE' to list.")
    args = ap.parse_args(argv)

    lode = load_lode_properties(Path(args.lode_file))
    print("=" * 72)
    print("DRY RUN — MarketWatch -> LODE.  READ-ONLY.  No writes, no Claude calls.")
    print("=" * 72)
    print(f"LODE properties on disk: {len(lode)} distinct  ({args.lode_file})")

    if args.mw_db:
        mw = load_mw_from_db(args.mw_db, args.min_materiality)
        src = "MarketWatch DB (read-only SELECT)"
    elif args.mw_file:
        mw = load_mw_from_file(Path(args.mw_file))
        src = f"offline file ({args.mw_file})"
    else:
        ap.error("give --mw-db (Cloud Shell) or --mw-file (offline)")
        return 2

    new_to_lode: list[MWStory] = []
    already: list[tuple[MWStory, str]] = []
    unresolved: list[MWStory] = []
    for s in mw:
        if not s.asset_name:
            unresolved.append(s)
            continue
        hit = best_match(s.asset_name, lode)
        if hit:
            already.append((s, hit))
        else:
            new_to_lode.append(s)

    total = len(mw)
    print(f"MW royalty_change stories (materiality >= {args.min_materiality}): {total}   [{src}]")
    print("-" * 72)
    print(f"  NEW to LODE (property not tracked): {len(new_to_lode)}")
    print(f"  already in LODE (possible update):  {len(already)}")
    print(f"  unresolved (no asset resolved):     {len(unresolved)}")

    # Break the headline set down by what MarketWatch itself claimed the exposure was.
    if new_to_lode:
        from collections import Counter

        by_exp = Counter((s.claimed_exposure or "unknown") for s in new_to_lode)
        print("\n  NEW-to-LODE by MarketWatch's claimed_exposure:")
        for exp, n in by_exp.most_common():
            print(f"     {n:>3}  {exp}")

        print(f"\n  Sample of NEW-to-LODE royalty events (first {args.sample}):")
        for s in new_to_lode[: args.sample]:
            when = (s.published_at or "")[:10]
            angle = (s.angle or "").replace("\n", " ")[:70]
            print(f"     [{when}] {(s.asset_name or '?')[:32]:32} | {(s.company_name or '?')[:22]:22} | {angle}")

    print("\n" + "=" * 72)
    if total:
        pct = 100 * len(new_to_lode) / total
        print(f"HEADLINE: MarketWatch surfaced {len(new_to_lode)} royalty-change events "
              f"({pct:.0f}% of {total}) on properties LODE does NOT yet track —")
        print("          live origination signal the technical-report feed is missing.")
    else:
        print("HEADLINE: no royalty_change stories at this materiality floor — lower --min-materiality.")
    print("Phase 2 would re-extract each into a LODE instrument candidate (still human-gated).")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())

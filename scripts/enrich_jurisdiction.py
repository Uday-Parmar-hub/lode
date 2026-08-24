"""Structured jurisdiction enrichment (Matt feedback, bucket B).

    python scripts/enrich_jurisdiction.py            # build the reviewable map (no DB writes)
    python scripts/enrich_jurisdiction.py --apply    # apply data/jurisdiction_map.json to the DB

The `jurisdiction` column is free text pulled from the source reports ("Sonora, Mexico",
"Chile (Antofagasta Region)", "Yukon / British Columbia, Canada"). This derives structured
country + primary state/province from it (Claude, one pass over the DISTINCT strings), then
computes continent (deterministic country->continent map) and jurisdiction_tier (1=US/CA/AU;
2/3 pending Matt's list).

Same philosophy as scripts/resolve_holders.py: the default run only writes a proposal ledger at
data/jurisdiction_map.json — a human can review/edit it — and nothing touches the DB until --apply.
Additive only: it fills the new country/state_province/continent/jurisdiction_tier columns and
never modifies the free-text `jurisdiction` or any human-review field.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import anthropic  # noqa: E402

from techreport import config, db  # noqa: E402

LEDGER = config.ROOT / "data" / "jurisdiction_map.json"
MODEL = "claude-opus-5"
TIER1 = {"United States", "Canada", "Australia"}

# canonical country -> continent (mining-relevant set; unknowns are flagged for review, never guessed)
CONTINENT = {
    # North America
    "Canada": "North America", "United States": "North America", "Mexico": "North America",
    "Guatemala": "North America", "Panama": "North America", "Dominican Republic": "North America",
    "Nicaragua": "North America", "Honduras": "North America",
    # South America
    "Chile": "South America", "Argentina": "South America", "Brazil": "South America",
    "Peru": "South America", "Colombia": "South America", "Ecuador": "South America",
    "Guyana": "South America", "Bolivia": "South America", "Suriname": "South America",
    "Venezuela": "South America", "Uruguay": "South America", "Paraguay": "South America",
    # Africa
    "Ghana": "Africa", "Mali": "Africa", "Mauritania": "Africa", "South Africa": "Africa",
    "Nigeria": "Africa", "Senegal": "Africa", "Tanzania": "Africa", "Madagascar": "Africa",
    "Burkina Faso": "Africa", "Côte d'Ivoire": "Africa", "Ivory Coast": "Africa",
    "Democratic Republic of the Congo": "Africa", "Zambia": "Africa", "Zimbabwe": "Africa",
    "Namibia": "Africa", "Botswana": "Africa", "Egypt": "Africa", "Morocco": "Africa",
    "Guinea": "Africa", "Sierra Leone": "Africa", "Liberia": "Africa", "Sudan": "Africa",
    "Ethiopia": "Africa", "Kenya": "Africa", "Eritrea": "Africa", "Gabon": "Africa",
    # Oceania
    "Australia": "Oceania", "New Zealand": "Oceania", "Papua New Guinea": "Oceania", "Fiji": "Oceania",
    # Asia
    "China": "Asia", "Philippines": "Asia", "Indonesia": "Asia", "India": "Asia",
    "Kazakhstan": "Asia", "Mongolia": "Asia", "Russia": "Asia", "Saudi Arabia": "Asia",
    "Laos": "Asia", "Vietnam": "Asia", "Myanmar": "Asia", "Japan": "Asia",
    "South Korea": "Asia", "Turkey": "Asia", "Kyrgyzstan": "Asia", "Uzbekistan": "Asia",
    # Europe
    "Finland": "Europe", "Sweden": "Europe", "Greece": "Europe", "Spain": "Europe",
    "Portugal": "Europe", "Serbia": "Europe", "Ireland": "Europe", "Norway": "Europe",
    "Poland": "Europe", "Romania": "Europe", "Bulgaria": "Europe", "United Kingdom": "Europe",
    "Germany": "Europe", "France": "Europe", "Italy": "Europe", "Kosovo": "Europe",
}

# fold common variants to the canonical name so tier/continent lookups are stable
ALIAS = {
    "USA": "United States", "U.S.A.": "United States", "U.S.": "United States", "US": "United States",
    "America": "United States", "UK": "United Kingdom", "DRC": "Democratic Republic of the Congo",
    "México": "Mexico", "Türkiye": "Turkey",
}

SYSTEM = """You normalize free-text mining-project locations into structured fields.

For each input string, return:
- country: the canonical English country name (e.g. "Canada", "United States", "Australia", "Mexico",
  "Chile", "Democratic Republic of the Congo"). Use "United States" (not "USA"). If the string spans
  MULTIPLE countries, use the PRIMARY / first-listed one. Empty string only if truly no country is present.
- state_province: the primary state / province / territory / region, cleaned up (e.g. "Ontario",
  "Nevada", "Western Australia", "Antofagasta"). Drop county/district/municipality granularity when a
  state/province is also present (e.g. "Eureka County, Nevada, USA" -> "Nevada"). Use null when the
  string is country-level only ("Chile", "Ghana"), or when it spans several states/provinces.

Return one object per input, echoing the exact input in "raw". Do not add or drop items."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _canon(country: str) -> str:
    country = (country or "").strip()
    return ALIAS.get(country, country)


def normalize(raws: list[str]) -> list[dict]:
    """Ask Claude to parse each distinct jurisdiction string into country + state_province."""
    tool = {
        "name": "emit",
        "description": "Return the normalized location for every input string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "raw": {"type": "string"},
                            "country": {"type": "string"},
                            "state_province": {"type": ["string", "null"]},
                        },
                        "required": ["raw", "country", "state_province"],
                    },
                }
            },
            "required": ["items"],
        },
    }
    payload = "\n".join(f"{i+1}. {s}" for i, s in enumerate(raws))
    msg = _client().messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "emit"},
        messages=[{"role": "user", "content": f"Normalize these {len(raws)} locations:\n\n{payload}"}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input["items"]
    raise RuntimeError("model returned no tool_use block")


def build() -> None:
    """Generate the reviewable jurisdiction map (no DB writes)."""
    with db.connect() as conn:
        rows = conn.execute(
            "select distinct jurisdiction from royalties where jurisdiction is not null order by 1"
        ).fetchall()
    raws = [r[0] for r in rows]
    print(f"distinct jurisdiction strings: {len(raws)}")

    parsed = {p["raw"]: p for p in normalize(raws)}
    missing = [s for s in raws if s not in parsed]
    if missing:
        print(f"  retrying {len(missing)} the model dropped...")
        parsed.update({p["raw"]: p for p in normalize(missing)})

    ledger, unmapped_countries = [], set()
    for raw in raws:
        p = parsed.get(raw, {})
        country = _canon(p.get("country", ""))
        state = p.get("state_province") or None
        continent = CONTINENT.get(country)
        if country and continent is None:
            unmapped_countries.add(country)
            continent = "?"  # flag for review rather than silently guessing
        tier = 1 if country in TIER1 else None
        ledger.append({
            "raw": raw, "country": country or None, "state_province": state,
            "continent": continent, "jurisdiction_tier": tier,
        })

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {LEDGER}  ({len(ledger)} entries)")
    if unmapped_countries:
        print(f"  ⚠ countries not in the continent map (continent='?', please review): "
              f"{sorted(unmapped_countries)}")
    _summary(ledger)
    print("\nReview data/jurisdiction_map.json, then:  python scripts/enrich_jurisdiction.py --apply")


def _summary(ledger: list[dict]) -> None:
    from collections import Counter
    cont = Counter(e["continent"] for e in ledger)
    tier = Counter(e["jurisdiction_tier"] for e in ledger)
    print("  by continent:", dict(cont))
    print("  tier-1 strings:", tier.get(1, 0), " | untier'd (pending Matt):", tier.get(None, 0))


def apply() -> None:
    """Apply data/jurisdiction_map.json to the new columns. Additive; pauses updated_at trigger."""
    if not LEDGER.exists():
        sys.exit("no ledger — run without --apply first, then review it.")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            n = 0
            for e in ledger:
                cur.execute(
                    "update royalties set country=%s, state_province=%s, continent=%s, "
                    "jurisdiction_tier=%s where jurisdiction=%s",
                    (e["country"], e["state_province"], e["continent"], e["jurisdiction_tier"], e["raw"]),
                )
                n += cur.rowcount
        finally:
            cur.execute("alter table royalties enable trigger trg_roy_touch")
        conn.commit()
        print(f"applied to {n} rows.")
        for label, q in [
            ("by continent", "select coalesce(continent,'(none)'), count(*) from royalties "
                             "group by 1 order by 2 desc"),
            ("tier-1 rows", "select count(*) from royalties where jurisdiction_tier=1"),
            ("top countries", "select country, count(*) from royalties where country is not null "
                              "group by 1 order by 2 desc limit 10"),
            ("still unmapped (jurisdiction set, country null)",
             "select count(*) from royalties where jurisdiction is not null and country is null"),
        ]:
            print(f"\n{label}:")
            for r in cur.execute(q).fetchall():
                print("  ", *r)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="apply the ledger to the DB (default: build ledger only)")
    args = ap.parse_args()
    apply() if args.apply else build()

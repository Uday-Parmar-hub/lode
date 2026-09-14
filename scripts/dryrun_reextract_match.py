"""DRY-RUN Phase 1.5 — re-extract MarketWatch royalty-change PROSE into (property, type, rate, holder)
with Claude, then match the property against LODE.

Why this exists: 90% of MarketWatch's royalty_change stories have no resolved sp_asset_id (the property
lives in the angle prose, not a structured column), so the naive field-join in dryrun_marketwatch_lode.py
can't place them. This reads the prose the way the real Phase-2 pipeline would, and gives the honest
"how many are genuinely new to LODE" number.

READ-ONLY: reads one local JSON export + the local LODE extraction, calls Claude for inference only,
writes NOTHING to either database. Output is a report to stdout.

    python dryrun_reextract_match.py --mw-file ~/Downloads/mw_royalty_changes.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anthropic

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dryrun_marketwatch_lode import LODE_PILOT, best_match, load_lode_properties  # noqa: E402

MODEL = "claude-sonnet-4-6"
CHUNK = 20

_SYS = (
    "You extract royalty facts from one-line mining news summaries. For each summary identify: the "
    "specific named property/project/claims the royalty is ON (NOT the company that holds or grants it, "
    "unless no property is named); the royalty type (NSR/GSR/NPI/stream/royalty, etc.); the rate exactly "
    "as stated; and who holds or receives the royalty. If no specific property is named (e.g. 'any "
    "properties X later acquires'), set property to null."
)

_TOOL = {
    "name": "emit",
    "description": "Extract the royalty's property and terms for each numbered summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "the summary's number"},
                        "property": {"type": ["string", "null"]},
                        "royalty_type": {"type": ["string", "null"]},
                        "rate": {"type": ["string", "null"]},
                        "holder": {"type": ["string", "null"]},
                    },
                    "required": ["n", "property"],
                },
            }
        },
        "required": ["items"],
    },
}


def _api_key() -> str:
    for line in (HERE.parent / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("ANTHROPIC_API_KEY not found in .env")


def extract(angles: list[str]) -> list[dict | None]:
    """Batch the summaries through Claude's tool interface; return one extraction dict per angle."""
    client = anthropic.Anthropic(api_key=_api_key())
    out: list[dict | None] = [None] * len(angles)
    for i in range(0, len(angles), CHUNK):
        batch = angles[i : i + CHUNK]
        payload = "\n".join(f"{j + 1}. {a}" for j, a in enumerate(batch))
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=4000, temperature=0, system=_SYS, tools=[_TOOL],
                tool_choice={"type": "tool", "name": "emit"},
                messages=[{"role": "user", "content": f"Extract from these {len(batch)} summaries:\n\n{payload}"}],
            )
            items = next((b.input["items"] for b in msg.content if b.type == "tool_use"), [])
            for it in items:
                k = int(it.get("n", 0)) - 1
                if 0 <= k < len(batch):
                    out[i + k] = it
        except Exception as e:  # a batch failing shouldn't sink the whole run
            print(f"  batch {i}-{i + len(batch)} failed: {e}", flush=True)
        print(f"  extracted {min(i + CHUNK, len(angles))}/{len(angles)}", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-extract MW royalty prose and match to LODE (read-only).")
    ap.add_argument("--mw-file", required=True, help="local JSON export of royalty_change stories")
    ap.add_argument("--lode-file", default=str(LODE_PILOT))
    ap.add_argument("--sample", type=int, default=25)
    args = ap.parse_args(argv)

    lode = load_lode_properties(Path(args.lode_file))
    mw = json.loads(Path(args.mw_file).read_text(encoding="utf-8"))
    angles = [(r.get("angle") or "") for r in mw]
    print("=" * 72)
    print("DRY RUN Phase 1.5 — re-extract prose -> match LODE.  READ-ONLY, no DB writes.")
    print("=" * 72)
    print(f"LODE properties: {len(lode)}   |   MW royalty_change events: {len(mw)}")
    print("Re-extracting property/terms from the prose with Claude ...")
    ext = extract(angles)

    new_to_lode: list[dict] = []
    already: list[tuple[dict, str]] = []
    no_property: list[dict] = []
    for e in ext:
        prop = (e or {}).get("property")
        if not prop:
            no_property.append(e or {})
            continue
        hit = best_match(prop, lode)
        if hit:
            already.append((e, hit))
        else:
            new_to_lode.append(e)

    print("\n" + "-" * 72)
    print(f"  NEW to LODE (property extracted, not tracked): {len(new_to_lode)}")
    print(f"  already in LODE (property matches):            {len(already)}")
    print(f"  no specific property (framework/portfolio):    {len(no_property)}")
    print("-" * 72)

    if new_to_lode:
        print(f"\nNEW-to-LODE origination candidates (first {args.sample}):")
        for e in new_to_lode[: args.sample]:
            prop = str(e.get("property") or "?")[:34]
            rate = str(e.get("rate") or "?")[:9]
            rtype = str(e.get("royalty_type") or "")[:7]
            holder = str(e.get("holder") or "?")[:26]
            print(f"  {prop:34} | {rate:9} {rtype:7} | holder: {holder}")

    print("\n" + "=" * 72)
    print(f"HEADLINE: of {len(mw)} royalty-change events MarketWatch caught (~6 weeks), "
          f"{len(new_to_lode)} are NEW royalty instruments LODE does not have —")
    print(f"          plus {len(already)} updates to instruments it already tracks.")
    print("Phase 2 would stage each as a human-gated LODE candidate (origin='marketwatch').")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())

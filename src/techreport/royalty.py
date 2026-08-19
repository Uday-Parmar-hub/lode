"""Royalty extraction from technical-report text — the pilot that turns an archived 43-101 into the
origination DB's factual columns: the asset facts plus each THIRD-PARTY royalty burdening the property
(type, rate, holder, conditions) with the exact source sentence quoted for human verification.

Two-step so we don't ship a 300-page report to the model: (1) pull the passages around every royalty
mention (`royalty_passages`), (2) hand only those to Claude for structured extraction. Every royalty
carries a verbatim `quote`, and nothing is trusted until an analyst validates it (Matt's requirement).
"""
from __future__ import annotations

import json
import re

import anthropic
from pydantic import BaseModel, Field

from . import config

_MODEL = "claude-sonnet-4-6"  # per-document extraction tier (matches resolve.py); Opus-5 is the upgrade

# Terms that flag a royalty/encumbrance passage. Broad on purpose (recall first — precision is Claude's job).
_ROY_RE = re.compile(
    r"\broyalt|\bNSR\b|\bGSR\b|\bNPI\b|net smelter|net profit|gross overrid|gross revenue|"
    r"overriding royalty|advance (minimum )?royalt|\bstream(ing)?\b|metal stream|encumbranc|"
    r"underlying (agreement|royalt)|subject to a", re.I,
)


def royalty_passages(text: str, *, window: int = 1200, cap: int = 40000) -> str:
    """Concatenate the passages around every royalty mention (overlaps merged), capped. Keeps the
    royalty/agreements content without shipping the whole report to the model."""
    hits = [m.start() for m in _ROY_RE.finditer(text or "")]
    if not hits:
        return ""
    spans: list[list[int]] = []
    for h in hits:
        s, e = max(0, h - window), min(len(text), h + window)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], e)  # merge overlapping window
        else:
            spans.append([s, e])
    return ("\n…\n".join(text[s:e] for s, e in spans))[:cap]


class Royalty(BaseModel):
    royalty_type: str | None = Field(None, description="NSR, GSR, NPI, gross revenue, metal stream, advance minimum royalty, etc.")
    rate: str | None = Field(None, description="the rate exactly as stated, e.g. '2%', '0.5-1.0%', 'US$5/oz'")
    holder: str | None = Field(None, description="the party ENTITLED to the royalty (the potential seller); null if the report doesn't name it")
    # ── structured royalty features (Matt's 7 columns) — extract each directly; null when not present ──
    partial_coverage: bool | None = Field(None, description="true only if the royalty burdens PART of the property (specific claims/ground), not the whole")
    advance_payments: str | None = Field(None, description="advance/minimum royalty or prepayment terms, stated briefly; null if none")
    production_threshold: str | None = Field(None, description="payable only above a stated production threshold; state it; null if none")
    production_cap: str | None = Field(None, description="capped after N units/$ or a fixed number of payments; state it; null if none")
    buyback: str | None = Field(None, description="buy-back / buy-down right (rate reducible on payment); state the terms; null if none")
    step_down: str | None = Field(None, description="sliding-scale / step-down (rate varies by price, grade, or time); state it; null if none")
    rofr: bool | None = Field(None, description="true only if a right of first refusal or first offer on the royalty is mentioned")
    other_terms: str | None = Field(None, description="any other material term not captured by the fields above; null if none")
    quote: str = Field(description="the exact verbatim sentence(s) from the report stating this royalty — never paraphrased")


class RoyaltyExtraction(BaseModel):
    project_name: str | None = None
    operator: str | None = None
    commodity: str | None = None
    jurisdiction: str | None = None
    stage: str | None = Field(None, description="exploration / PEA / PFS / FS / development / producing")
    has_third_party_royalty: bool
    royalties: list[Royalty]
    notes: str | None = Field(None, description="anything ambiguous a human should check (e.g. government/production royalties noted but excluded)")


_PROMPT = """You are extracting EXISTING third-party royalties from a mining technical report, for a \
royalty-ACQUISITION team. A third-party royalty is an encumbrance on THIS property held by a party \
OTHER than the current operator — e.g. "the Property is subject to a 2% NSR royalty held by X". These \
are potential acquisition targets.

Extract ONLY royalties that burden the property described in this report. Do NOT include:
- royalties the operator itself HOLDS on other properties,
- government/state/production/severance taxes or Crown royalties (mention them in `notes`, don't list them),
- proposed/hypothetical royalties not actually granted.

For each real royalty capture: type, rate (as stated), holder (the entitled party — the seller), and its \
STRUCTURED FEATURES as separate fields — partial_coverage (burdens only part of the property), \
advance_payments, production_threshold, production_cap, buyback (buy-back/buy-down right), step_down \
(sliding-scale/step-down), rofr (right of first refusal/offer), and other_terms — each null when not \
present. Always include the EXACT verbatim sentence(s) as `quote` (never paraphrase — the analyst verifies).

Also capture the asset facts: project_name, operator, commodity, jurisdiction, stage.

If the property has no third-party royalty, set has_third_party_royalty=false and royalties=[]. \
Operator hint (from our records, may be stale): {operator_hint}

REPORT PASSAGES (royalty-relevant excerpts):
{passages}"""


def extract(passages: str, operator_hint: str | None = None) -> RoyaltyExtraction:
    """Claude structured extraction over the royalty passages -> validated RoyaltyExtraction."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    schema = RoyaltyExtraction.model_json_schema()
    msg = client.messages.create(
        model=_MODEL, max_tokens=4000,
        tools=[{"name": "record_royalties",
                "description": "Record the third-party royalties + asset facts from this report.",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": "record_royalties"},
        messages=[{"role": "user",
                   "content": _PROMPT.format(operator_hint=operator_hint or "unknown", passages=passages)}],
    )
    block = next(b for b in msg.content if b.type == "tool_use")
    data = dict(block.input)
    # the richer nested schema sometimes makes the model serialize `royalties` as a JSON string —
    # coerce it back to a list before validation so the extraction never fails on that alone.
    if isinstance(data.get("royalties"), str):
        data["royalties"] = json.loads(data["royalties"])
    return RoyaltyExtraction.model_validate(data)

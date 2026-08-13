"""Load the OR portfolio (asset / operator / jurisdiction) that drives the corpus.

The corpus is fetched per-OPERATOR (a technical report is filed by the operator company, named by
the project) but organized per-ASSET, so this exposes both views. Real portfolio data → the xlsx is
never committed (read from $HOME via config.PORTFOLIO_XLSX).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from . import config


@dataclass(frozen=True)
class Asset:
    """One portfolio asset (one royalty/stream instrument on one asset)."""

    stage: str
    asset: str
    operator: str
    jurisdiction: str
    instrument: str


def load_assets() -> list[Asset]:
    """Read the portfolio xlsx into Asset rows (Portfolio sheet)."""
    df = pd.read_excel(config.PORTFOLIO_XLSX, sheet_name="Portfolio")
    df.columns = [str(c).strip().lower() for c in df.columns]
    rows: list[Asset] = []
    for _, r in df.iterrows():
        asset = str(r.get("asset", "")).strip()
        operator = str(r.get("operator", "")).strip()
        if not asset or asset.lower() == "nan":
            continue
        rows.append(
            Asset(
                stage=str(r.get("stage", "")).strip(),
                asset=asset,
                operator=operator,
                jurisdiction=str(r.get("jurisdiction", "")).strip(),
                instrument=str(r.get("instrument", "")).strip(),
            )
        )
    return rows


def by_operator(assets: list[Asset] | None = None) -> dict[str, list[Asset]]:
    """Group assets by operator — the unit the archiver fetches on (one fetch, many assets)."""
    assets = assets if assets is not None else load_assets()
    grouped: dict[str, list[Asset]] = defaultdict(list)
    for a in assets:
        grouped[a.operator].append(a)
    return dict(grouped)

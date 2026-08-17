"""Configuration — loads .env from the project root and exposes settings."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()

KSCOPE_API_KEY = os.environ.get("KSCOPE_API_KEY")
KSCOPE_BASE_URL = os.environ.get("KSCOPE_BASE_URL", "https://api.kscope.io")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# LSEG Global Filings — the deep-history filing source (see CLAUDE.md; trial creds).
LSEG_USERNAME = os.environ.get("LSEG_USERNAME")
LSEG_PASSWORD = os.environ.get("LSEG_PASSWORD")
LSEG_APP_KEY = os.environ.get("LSEG_APP_KEY")

# The portfolio drives the whole corpus (asset -> operator -> source). Real portfolio data, so the
# xlsx is NOT committed — it's read from its location in $HOME by default (override with env).
PORTFOLIO_XLSX = Path(
    os.environ.get("PORTFOLIO_XLSX", str(Path.home() / "OR_Portfolio_List_2026-08-04.xlsx"))
)

# Where downloaded technical reports land (gitignored — big + confidential).
CORPUS_DIR = Path(os.environ.get("CORPUS_DIR", str(ROOT / "corpus")))

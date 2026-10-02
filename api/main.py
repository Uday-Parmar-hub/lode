"""LODE ingestion service — the production front door for the MarketWatch "Add to LODE" button.

Why this exists as its own service. The button needs to extract royalties from a press release and
stage them, and that work is Python: passage windowing, the Claude extraction, the commodity parser,
and chain.link's identity assignment. The LODE dashboard is a Node image, so it cannot call any of
it. The alternative was re-implementing the extractor in TypeScript inside the dashboard, which
would have left two extractors drifting apart — the exact duplication that produced the dup-key,
commodity-parser and quote-verification bugs this repo has spent weeks consolidating away.

So: one small HTTP wrapper around scripts/add_one_to_lode.ingest_one, which is the same code the CLI
and the tests exercise. No second implementation of anything.

Deployed to the same Container Apps environment as the dashboards with INTERNAL ingress, so it is
reachable only from inside that environment and never from the internet.

    uvicorn api.main:app --port 8080
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import pathlib
import sys

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Uvicorn configures handlers for its own loggers only, so a bare getLogger() here writes to the
# root logger, which has none — every log.info below was being dropped on the floor, and the only
# trace of an ingest in the container logs was uvicorn's access line. Without this you cannot tell a
# call that staged four royalties from one that found none, which is exactly the question you ask
# the logs. basicConfig is a no-op if the host already configured logging.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lode.ingest")

# scripts/ is not a package; load the module by path (the tests do the same).
_spec = importlib.util.spec_from_file_location("add_one_to_lode", ROOT / "scripts" / "add_one_to_lode.py")
assert _spec and _spec.loader
add_one = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_one)

app = FastAPI(title="LODE ingestion", docs_url=None, redoc_url=None, openapi_url=None)

# The app role MarketWatch's managed identity was granted. Easy Auth proves the caller holds a valid
# token for this app; this proves it is the caller we actually authorised, rather than any identity
# in the tenant that happens to have one.
REQUIRED_ROLE = "Royalties.Ingest"


class Release(BaseModel):
    docid: str
    text: str
    company: str | None = None
    date: str | None = None
    url: str | None = None
    actor: str | None = None


def _roles(principal: str | None) -> list[str]:
    """App roles from Easy Auth's base64 claims header."""
    if not principal:
        return []
    try:
        claims = json.loads(base64.b64decode(principal).decode("utf-8")).get("claims", [])
        return [c.get("val") for c in claims if c.get("typ") == "roles"]
    except Exception:
        return []


def _authorize(principal: str | None) -> None:
    """Require the caller to hold REQUIRED_ROLE.

    Skipped only when LODE_INGEST_ALLOW_ANON=1, which exists for local runs where there is no Easy
    Auth in front to inject the header. It must never be set in production — the service would then
    accept any caller that can reach it.
    """
    if os.getenv("LODE_INGEST_ALLOW_ANON") == "1":
        return
    if REQUIRED_ROLE not in _roles(principal):
        # Deliberately terse: tell an authorised caller nothing it does not already know.
        raise HTTPException(status_code=403, detail="caller lacks the required app role")


@app.get("/health")
def health() -> dict:
    """Liveness only — no database call, so a database blip cannot take the revision down."""
    return {"ok": True, "service": "lode-ingest"}


@app.post("/ingest")
def ingest(release: Release, x_ms_client_principal: str | None = Header(default=None)) -> dict:
    """Extract one press release and stage its royalties as pending rows for review.

    Idempotent per release: a docid already present returns {"already": true} without re-extracting,
    so a retry or a double-click costs nothing and cannot duplicate.
    """
    _authorize(x_ms_client_principal)
    try:
        result = add_one.ingest_one(release.model_dump())
    except Exception as e:                                    # noqa: BLE001 — surface, don't leak
        log.exception("ingest failed for %s", release.docid)
        raise HTTPException(status_code=500, detail=f"ingestion failed: {type(e).__name__}") from e
    # `extracted` and `reason` separate the two ways a call can stage nothing: the passage filter
    # found no royalty language (Claude was never called) versus Claude read it and found no royalty.
    # Both show the analyst "No royalty found"; only the log says which, and that is the difference
    # between a prompt problem and a filter problem.
    log.info("ingested %s -> %s", release.docid,
             {k: result.get(k) for k in ("inserted", "extracted", "skipped", "already", "reason")})
    return result

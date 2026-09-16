"""CI-safe tests for the deterministic parts of the MarketWatch -> LODE bridge.

No Claude calls here — the LLM extraction quality is graded separately by scripts/eval_extraction.py.
These guard the parts that must be exactly right every time: commodity/rate parsing, and idempotency
(a story already in LODE is never re-extracted or duplicated).

    python -m pytest tests/test_bridge.py -v

The DB-backed tests use TEST_DATABASE_URL (default: the local lode_test clone) and SKIP cleanly if it
is unreachable, so the pure-function tests still run anywhere.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://lode:lode@localhost:5433/lode_test")
HELPER = ROOT / "scripts" / "add_one_to_lode.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # main() is __main__-guarded, so import is side-effect-free
    return mod


add_one = _load_module("add_one_to_lode", HELPER)


# ---------------------------------------------------------------- pure functions (always run)
@pytest.mark.parametrize("text,expected", [
    ("gold", ["Au"]),
    ("gold and copper", ["Au", "Cu"]),
    ("Au, Ag", ["Au", "Ag"]),
    ("copper/gold", ["Cu", "Au"]),
    ("lithium", ["Li"]),
    ("gold and gold", ["Au"]),   # de-duped
    ("", []),
])
def test_commodities(text, expected):
    assert add_one.commodities(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2%", 2.0),
    ("2.0%", 2.0),
    ("0.5%", 0.5),
    ("1.5% (vendor)", 1.5),
    ("US$5/t", None),   # a per-tonne figure is not a % rate
    ("100%", None),     # a buyout / stream, not an NSR rate
    (None, None),
    ("", None),
])
def test_rate_pct(text, expected):
    assert add_one.rate_pct(text) == expected


# ---------------------------------------------------------------- DB-backed (skip if no DB)
def _connect():
    try:
        import psycopg
        return psycopg.connect(TEST_DB, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test DB not reachable: {exc}")


def _run_helper(payload: dict) -> dict:
    """Invoke add_one_to_lode.py as a subprocess against TEST_DB; return its JSON result."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        tmp = f.name
    try:
        env = {**os.environ, "DATABASE_URL": TEST_DB}
        proc = subprocess.run([sys.executable, str(HELPER), tmp], capture_output=True, text=True,
                              env=env, cwd=str(ROOT), timeout=90)
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    finally:
        os.unlink(tmp)


def test_idempotent_when_story_already_in_lode():
    """A story whose docid is already present must return already=True without inserting a duplicate
    (and without a Claude call — the check short-circuits before extraction)."""
    conn = _connect()
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO royalties (project_name, regime, source_docid, status, ingested_from, commodity)"
                " VALUES ('Pytest Project', 'MarketWatch', %s, 'pending', 'marketwatch', '{}')",
                (docid,),
            )
        conn.commit()

        res = _run_helper({"docid": docid, "company": "X",
                           "text": "a 2% NSR royalty held by SomeCo on the Foo project"})
        assert res.get("already") is True
        assert res.get("inserted") == 0

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM royalties WHERE source_docid = %s", (docid,))
            assert cur.fetchone()[0] == 1  # still exactly one row, no duplicate
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM royalties WHERE source_docid = %s", (docid,))
        conn.commit()
        conn.close()


def test_no_royalty_passage_inserts_nothing():
    """A release with no royalty language yields no passage -> no Claude call, no row."""
    conn = _connect()
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    try:
        res = _run_helper({"docid": docid, "company": "X",
                           "text": "The company drilled 12 metres at 8 g/t gold, extending the zone along strike."})
        assert res.get("inserted") == 0
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM royalties WHERE source_docid = %s", (docid,))
            assert cur.fetchone()[0] == 0
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM royalties WHERE source_docid = %s", (docid,))
        conn.commit()
        conn.close()

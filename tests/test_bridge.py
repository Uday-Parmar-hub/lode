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
from techreport import chain  # noqa: E402  (shared identity/memory-chain logic)


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


# ---------------------------------------------------------------- memory chain (skip if no DB)
# These guard the contract that makes a bridged royalty a first-class instrument rather than an
# orphan row. They run entirely inside a transaction that is rolled back, so they never leave data
# behind, and they make no Claude call.

EDIT_INSERT = """
INSERT INTO royalties (project_name, operator, commodity, jurisdiction, stage, regime,
  source_label, source_url, source_date, source_quote, quote_verified, ingested_from, dup_key,
  instrument_id, royalty_type, rate, rate_pct, holder,
  source_docid, origin, status, is_primary, needs_revalidation, created_at, updated_at)
SELECT project_name, operator, commodity, jurisdiction, stage, regime,
  source_label, source_url, source_date, source_quote, quote_verified, ingested_from, dup_key,
  instrument_id, royalty_type, rate, rate_pct, %s,
  coalesce(source_docid,'manual') || '#edit-' || extract(epoch from now())::bigint,
  'claude_human_edited', 'pending', true, true, now(), now()
FROM royalties WHERE id = %s RETURNING id, instrument_id"""


@pytest.fixture
def tx():
    """A cursor whose transaction is always rolled back — these tests must not mutate the DB."""
    conn = _connect()
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


def _roy(**kw) -> dict:
    """Parameters for add_one_to_lode's INSERT, with only the fields a test cares about set."""
    base = dict(project_name=None, operator=None, commodity=[], jurisdiction=None, stage=None,
                is_producing=None,
                royalty_type=None, rate=None, rate_pct=None, holder=None, partial_coverage=None,
                advance_payments=None, production_threshold=None, production_cap=None, buyback=None,
                step_down=None, rofr=None, features_note=None, regime="MarketWatch", source_docid=None,
                source_label="MarketWatch · test", source_url=None, source_date=None,
                source_quote="test quote", quote_verified=False)
    base.update(kw)
    return base


def test_inserted_row_is_linked_into_the_memory_chain(tx):
    """A bridged row must come out with dup_key, instrument_id and origin set. A NULL instrument_id
    is what breaks the dashboard's edit path (see the test below), so this is load-bearing."""
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    tx.execute(add_one.INSERT, _roy(project_name="Chain Test Project", holder="Someone Ltd.",
                                    royalty_type="NSR", rate="2%", rate_pct=2, source_docid=docid))
    chain.link(tx, "source_docid = %s", (docid,))
    tx.execute("select instrument_id, dup_key, origin, is_primary from royalties where source_docid=%s",
               (docid,))
    instrument_id, dup_key, origin, is_primary = tx.fetchone()
    assert instrument_id and instrument_id.startswith("inst_")
    assert dup_key == "chaintestproject|NSR|2|someone"
    assert origin == "marketwatch"   # migration 003's convention for ingested_from='marketwatch'
    assert is_primary is True


def test_bridged_row_joins_an_existing_instrument(tx):
    """The point of the bridge: when LODE already holds this royalty from a technical report, the
    press release must corroborate that instrument rather than mint a second one."""
    report_doc, pr_doc = f"rpt-{uuid.uuid4().hex[:8]}", f"mw-{uuid.uuid4().hex[:8]}"
    shared = dict(project_name="Joined Project", holder="Alaska Hardrock, Inc.",
                  royalty_type="NSR", rate="2%", rate_pct=2)
    tx.execute(add_one.INSERT, _roy(source_docid=report_doc, **shared))
    chain.link(tx, "source_docid = %s", (report_doc,))
    tx.execute("select instrument_id from royalties where source_docid=%s", (report_doc,))
    existing = tx.fetchone()[0]

    tx.execute(add_one.INSERT, _roy(source_docid=pr_doc, **shared))
    linked = chain.link(tx, "source_docid = %s", (pr_doc,))
    tx.execute("select instrument_id from royalties where source_docid=%s", (pr_doc,))
    assert tx.fetchone()[0] == existing          # same real-world royalty -> one instrument
    assert linked["joined_existing"] == 1


def test_first_edit_leaves_exactly_one_primary(tx):
    """Replays the dashboard's saveFactEdit. Its demote runs `WHERE instrument_id = <id>`, so with a
    linked row the prior version is retired and exactly one version stays current."""
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    tx.execute(add_one.INSERT, _roy(project_name="Edit Test Project", holder="Holder Ltd.",
                                    royalty_type="NSR", rate="2%", rate_pct=2, source_docid=docid))
    chain.link(tx, "source_docid = %s", (docid,))
    tx.execute("select id, dup_key from royalties where source_docid=%s", (docid,))
    rid, dup_key = tx.fetchone()

    tx.execute(EDIT_INSERT, ("Holder Ltd. (corrected)", rid))
    new_id, new_iid = tx.fetchone()
    tx.execute("UPDATE royalties SET is_primary = (id = %s) WHERE instrument_id = %s", (new_id, new_iid))
    tx.execute("select count(*) from royalties where dup_key=%s and is_primary", (dup_key,))
    assert tx.fetchone()[0] == 1


def test_edit_duplicates_when_instrument_id_is_missing(tx):
    """The regression this all exists to prevent: with a NULL instrument_id the demote matches nothing,
    so the analyst's first correction leaves TWO current versions of one royalty. Documents exactly
    why _link_into_memory_chain must run on every insert."""
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    tx.execute(add_one.INSERT, _roy(project_name="Orphan Test Project", holder="Holder Ltd.",
                                    royalty_type="NSR", rate="2%", rate_pct=2, source_docid=docid))
    chain.link(tx, "source_docid = %s", (docid,))
    tx.execute("select id, dup_key from royalties where source_docid=%s", (docid,))
    rid, dup_key = tx.fetchone()
    tx.execute("update royalties set instrument_id = null where id=%s", (rid,))  # simulate the old insert

    tx.execute(EDIT_INSERT, ("Holder Ltd. (corrected)", rid))
    new_id, new_iid = tx.fetchone()
    tx.execute("UPDATE royalties SET is_primary = (id = %s) WHERE instrument_id = %s", (new_id, new_iid))
    tx.execute("select count(*) from royalties where dup_key=%s and is_primary", (dup_key,))
    assert tx.fetchone()[0] == 2   # the bug, pinned so a regression is loud


def test_royalties_differing_only_in_rate_both_store(tx):
    """Migration 007: `rate` is part of the uniqueness key, so two royalties that differ only in
    rate are two instruments and both land.

    This test asserted the opposite until 007 — that the second was dropped — which is what the
    constraint did, and it was wrong. collapse_repeats() already keyed on rate and every structured
    term, so the index was narrower than the rule meant to back it up and silently discarded
    royalties collapse_repeats had deliberately kept."""
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    stored = skipped = 0
    for rate, pct in (("2%", 2), ("1%", 1)):
        tx.execute(add_one.INSERT, _roy(project_name="Collide Project", holder="Newmont Corporation",
                                        royalty_type="NSR", rate=rate, rate_pct=pct, source_docid=docid))
        if tx.rowcount == 1:
            stored += 1
        else:
            skipped += 1
    assert (stored, skipped) == (2, 0)
    tx.execute("select count(*) from royalties where source_docid=%s", (docid,))
    assert tx.fetchone()[0] == 2


def test_identical_royalty_is_still_reported_as_skipped(tx):
    """007 widened the key; it did not remove it. A genuinely identical royalty must still be
    refused, and the caller must count by rowcount rather than by rows attempted — otherwise a
    royalty the database dropped reads back to the analyst as a success."""
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    stored = skipped = 0
    for _ in range(2):
        tx.execute(add_one.INSERT, _roy(project_name="Same Project", holder="Newmont Corporation",
                                        royalty_type="NSR", rate="2%", rate_pct=2, source_docid=docid))
        if tx.rowcount == 1:
            stored += 1
        else:
            skipped += 1
    assert (stored, skipped) == (1, 1)
    tx.execute("select count(*) from royalties where source_docid=%s", (docid,))
    assert tx.fetchone()[0] == 1


def test_same_royalty_on_two_properties_both_store(tx):
    """The Metalore case: one release, an NSR on each of two properties, identical in every other
    respect. They are separate instruments on separate ground and both must land.

    Before Royalty.property_name every royalty from a document inherited one project_name, so these
    two arrived identical, the index refused the second, and the release reported "Added 1 of 2"
    with no way to recover the other — a later click finds the stored row and returns `already`."""
    docid = f"pytest-{uuid.uuid4().hex[:8]}"
    for project in ("Cherbourg-Foxear", "Walters-Leduc-Legault"):
        tx.execute(add_one.INSERT, _roy(project_name=project, holder="Metalore Resources",
                                        royalty_type="NSR", rate="1.5%", rate_pct=1.5,
                                        source_docid=docid))
        assert tx.rowcount == 1, f"{project} was refused"
    tx.execute("select count(*) from royalties where source_docid=%s", (docid,))
    assert tx.fetchone()[0] == 2


def test_landing_on_a_validated_instrument_requests_revalidation(tx):
    """Migration 003's contract: a new source arriving on an already-validated instrument goes back
    for re-review rather than quietly changing what the desk already signed off."""
    report_doc, pr_doc = f"rpt-{uuid.uuid4().hex[:8]}", f"mw-{uuid.uuid4().hex[:8]}"
    shared = dict(project_name="Validated Project", holder="Holder Ltd.",
                  royalty_type="NSR", rate="2%", rate_pct=2)
    tx.execute(add_one.INSERT, _roy(source_docid=report_doc, **shared))
    chain.link(tx, "source_docid = %s", (report_doc,))
    tx.execute("update royalties set status='validated' where source_docid=%s", (report_doc,))

    tx.execute(add_one.INSERT, _roy(source_docid=pr_doc, **shared))
    linked = chain.link(tx, "source_docid = %s", (pr_doc,))
    assert linked["flagged_revalidation"] >= 1
    tx.execute("select count(*) from royalties where source_docid in (%s,%s) and needs_revalidation",
               (report_doc, pr_doc))
    assert tx.fetchone()[0] >= 1


# ---------------------------------------------------------------- in-batch repeat collapse (pure)
def _r(project, holder, rtype, rate, **extra):
    row = {"project_name": project, "holder": holder, "royalty_type": rtype, "rate": rate}
    row.update(extra)
    return row


def test_collapse_repeats_drops_verbatim_repeats():
    rows = [_r("Foo", "AngloGold", "NSR", "2%"), _r("Foo", "AngloGold", "NSR", "2%")]
    kept, dropped = add_one.collapse_repeats(rows)
    assert (len(kept), dropped) == (1, 1)


def test_collapse_repeats_matches_null_holders():
    """The case that actually bit the corpus: a NULL holder is invisible to the unique index, so the
    same royalty re-inserted on every load (Salares Norte's 2% NSR: 14 rows from one document)."""
    rows = [_r("Salares Norte", None, "NSR", "2%"), _r("Salares Norte", None, "NSR", "2%")]
    kept, dropped = add_one.collapse_repeats(rows)
    assert (len(kept), dropped) == (1, 1)


def test_collapse_repeats_keeps_different_rates():
    """A royalty stack is not a duplicate — different rates are different instruments."""
    rows = [_r("Foo", "Vendors", "NSR", "2%"), _r("Foo", "Vendors", "NSR", "1%")]
    kept, dropped = add_one.collapse_repeats(rows)
    assert (len(kept), dropped) == (2, 0)


def test_collapse_repeats_keeps_same_rate_with_different_terms():
    """Two royalties can share a rate and differ in their terms — one capped, one not. Those are
    different instruments, so the collapse key must include the structured feature fields."""
    rows = [_r("Foo", "Vendors", "NSR", "2%", production_cap="1Moz"),
            _r("Foo", "Vendors", "NSR", "2%", production_cap=None)]
    kept, dropped = add_one.collapse_repeats(rows)
    assert (len(kept), dropped) == (2, 0)


# ---------------------------------------------------------------- primary selection across sources
def _two_source_instrument(tx, report_status: str):
    """A validated-or-not technical report and a bridged press release sharing one instrument."""
    tag = uuid.uuid4().hex[:8]
    proj = f"Lineage {tag}"
    ins = ("insert into royalties (project_name, holder, royalty_type, rate, rate_pct, regime,"
           " source_docid, source_date, status, ingested_from, commodity, quote_verified, is_primary)"
           " values (%s,'Holder Ltd.','NSR','2%%',2,%s,%s,%s,%s,%s,'{}',true,true) returning id")
    tx.execute(ins, (proj, "NI 43-101", f"rpt-{tag}", "2024-01-01", report_status, "edgar"))
    report = tx.fetchone()[0]
    tx.execute(ins, (proj, "MarketWatch", f"mw-{tag}", "2026-09-23", "pending", "marketwatch"))
    pr = tx.fetchone()[0]
    chain.link(tx, "source_docid = any(%s)", ([f"rpt-{tag}", f"mw-{tag}"],))
    return proj, report, pr


def _edit(tx, row_id: int) -> int:
    tx.execute(EDIT_INSERT, ("Holder Ltd. (corrected)", row_id))
    return tx.fetchone()[0]


def test_newest_version_is_shown_whether_or_not_it_is_validated(tx):
    """docs/specs/memory_chain.md, locked decision: "Newest shown, validated trusted." The surfaced
    row is the newest version even when an older one from another source has been signed off — the
    needs_revalidation badge is what tells the analyst not to trust it blindly. Ranking validated
    first instead would hide the fact that a newer source changed something, which is the whole
    reason the version chain exists."""
    proj, report, pr = _two_source_instrument(tx, "validated")   # report 2024 validated, PR 2026 pending
    chain.set_primary(tx, "project_name = %s", (proj,))
    tx.execute("select is_primary from royalties where id = %s", (pr,))
    assert tx.fetchone()[0] is True            # the newer press release is surfaced
    tx.execute("select is_primary from royalties where id = %s", (report,))
    assert tx.fetchone()[0] is False           # the validated report is retained as history, not lost
    tx.execute("select count(*) from royalties where project_name = %s", (proj,))
    assert tx.fetchone()[0] == 2               # nothing was deleted


def test_an_edit_supersedes_its_own_previous_version(tx):
    """An analyst correction appends a version and that version becomes the one shown for its source
    lineage; the row it corrected is kept as history. This is what the lineage scoping is for — the
    demote must reach the edited row's own lineage and stop there, rather than the whole instrument."""
    proj, report, pr = _two_source_instrument(tx, "pending")
    new_id = _edit(tx, pr)
    chain.set_primary(tx, "project_name = %s", (proj,))
    tx.execute("select is_primary from royalties where id = %s", (new_id,))
    assert tx.fetchone()[0] is True            # the correction is shown
    tx.execute("select is_primary from royalties where id = %s", (pr,))
    assert tx.fetchone()[0] is False           # the version it replaced is retained, not primary


def test_exactly_one_primary_per_group_after_selection(tx):
    """The invariant, whatever the inputs: never zero primaries, never two."""
    proj, report, pr = _two_source_instrument(tx, "pending")
    _edit(tx, pr)
    _edit(tx, report)
    chain.set_primary(tx, "project_name = %s", (proj,))
    tx.execute("""select count(*) from (
                    select dup_key, count(*) filter (where is_primary) n from royalties
                     where project_name = %s group by dup_key) g
                   where n <> 1""", (proj,))
    assert tx.fetchone()[0] == 0

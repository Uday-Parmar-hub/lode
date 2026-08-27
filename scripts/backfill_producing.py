"""Backfill is_producing from the already-extracted `stage` text (migration 004).

    python scripts/backfill_producing.py            # DRY RUN — show the mapping counts, commit nothing
    python scripts/backfill_producing.py --apply

Matt asked for a binary "producing / not" alongside the free-text stage. Rather than re-read ~700
reports, we derive it deterministically from the stage Claude already extracted: any stage mentioning
production -> true; a named pre-production stage (exploration / PEA / PFS / FS / development) -> false;
no stage -> left NULL (unknown, for a human to fill). The ILIKE '%produc%' match is exact here — no
other stage term contains that substring.

Only touches rows where is_producing IS NULL, so it never overwrites a value Claude extracts directly
going forward (idempotent, re-runnable). Additive, non-destructive; pauses the updated_at trigger so it
doesn't bump "Date Modified"; one txn, rolled back unless --apply.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import db  # noqa: E402

_PRODUCING = "%produc%"  # matches "producing" / "in production" / "production" — pass as a param, not inlined

SET_TRUE = "update royalties set is_producing=true where is_producing is null and stage ilike %s"
SET_FALSE = ("update royalties set is_producing=false where is_producing is null "
             "and stage is not null and stage not ilike %s")


def main(apply: bool) -> None:
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "select count(*) filter (where stage ilike %s), "
            "       count(*) filter (where stage is not null and stage not ilike %s), "
            "       count(*) filter (where stage is null) "
            "  from royalties where is_producing is null",
            (_PRODUCING, _PRODUCING),
        )
        prod, notprod, unknown = cur.fetchone()
        cur.execute("alter table royalties disable trigger trg_roy_touch")
        try:
            cur.execute(SET_TRUE, (_PRODUCING,))
            t = cur.rowcount
            cur.execute(SET_FALSE, (_PRODUCING,))
            f = cur.rowcount
        finally:
            cur.execute("alter table royalties enable trigger trg_roy_touch")
        print(f"is_producing backfill (NULL rows only):")
        print(f"  producing (stage ~ 'produc'): {t}")
        print(f"  not producing (named pre-production stage): {f}")
        print(f"  left unknown (no stage): {unknown}")
        if apply:
            conn.commit()
            print("COMMITTED.")
        else:
            conn.rollback()
            print("DRY RUN — rolled back. Re-run with --apply to commit.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)

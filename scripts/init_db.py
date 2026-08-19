"""(Re)create the LODE schema from db/schema.sql. DEV: drops the table + enums first so it's rerunnable.

    python scripts/init_db.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport import db  # noqa: E402

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "db" / "schema.sql"
DROP = """
DROP TABLE IF EXISTS royalties CASCADE;
DROP TYPE  IF EXISTS review_status CASCADE;
DROP TYPE  IF EXISTS availability CASCADE;
"""

with db.connect() as conn:
    with conn.cursor() as cur:
        cur.execute(DROP)
        cur.execute(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
print("schema applied:", SCHEMA)

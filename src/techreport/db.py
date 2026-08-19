"""Database connection for LODE. Local Postgres by default (docker-compose, port 5433); override with
DATABASE_URL for Azure later. Uses psycopg 3 — same driver as MarketWatch."""
from __future__ import annotations

import os

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lode:lode@localhost:5433/lode")


def connect() -> psycopg.Connection:
    """A new autocommit-off connection (caller commits)."""
    return psycopg.connect(DATABASE_URL)

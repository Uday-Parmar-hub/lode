#!/usr/bin/env bash
# Local Postgres for LODE via conda — no Docker, no sudo, all userspace. Start on :5433, create db 'lode'.
#   conda run -n mining_ai bash scripts/pg_local.sh          # start (idempotent)
#   conda run -n mining_ai bash scripts/pg_local.sh stop     # stop
set -e
PGDATA="$HOME/.lode_pg"; PORT=5433
if [ "$1" = "stop" ]; then pg_ctl -D "$PGDATA" stop -m fast; exit 0; fi

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  initdb -D "$PGDATA" -U "$USER" --auth-local=trust --auth-host=trust >/dev/null
fi
pg_ctl -D "$PGDATA" status >/dev/null 2>&1 || \
  pg_ctl -D "$PGDATA" -o "-p $PORT -k /tmp" -l "$PGDATA/server.log" -w start
until pg_isready -h localhost -p $PORT -q; do sleep 1; done
psql -h localhost -p $PORT -d postgres -U "$USER" -tAc "SELECT 1 FROM pg_roles WHERE rolname='lode'" | grep -q 1 || \
  psql -h localhost -p $PORT -d postgres -U "$USER" -c "CREATE ROLE lode LOGIN SUPERUSER PASSWORD 'lode'" >/dev/null
psql -h localhost -p $PORT -d postgres -U "$USER" -tAc "SELECT 1 FROM pg_database WHERE datname='lode'" | grep -q 1 || \
  psql -h localhost -p $PORT -d postgres -U "$USER" -c "CREATE DATABASE lode OWNER lode" >/dev/null
echo "LODE postgres ready on localhost:$PORT (db=lode)"

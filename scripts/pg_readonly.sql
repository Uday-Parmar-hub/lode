-- Read-only role for the AI text-to-SQL search path. The dashboard runs LLM-generated SELECTs as this
-- role, never as the owner (`lode`). Defense-in-depth: SELECT-only grant + every transaction forced
-- read-only + a 5s statement timeout. Even a validation bypass cannot write, create, or run long.
--   conda run -n mining_ai psql -h localhost -p 5433 -d lode -U lode -f scripts/pg_readonly.sql

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lode_ro') THEN
    CREATE ROLE lode_ro LOGIN PASSWORD 'lode_ro';
  END IF;
END $$;

-- strip anything the default/public grants may have handed it, then grant exactly SELECT on royalties
REVOKE ALL ON DATABASE lode FROM lode_ro;
REVOKE ALL ON SCHEMA public FROM lode_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM lode_ro;
GRANT CONNECT ON DATABASE lode TO lode_ro;
GRANT USAGE ON SCHEMA public TO lode_ro;
GRANT SELECT ON royalties TO lode_ro;

ALTER ROLE lode_ro SET default_transaction_read_only = on;
ALTER ROLE lode_ro SET statement_timeout = '5000ms';
ALTER ROLE lode_ro SET search_path = public;

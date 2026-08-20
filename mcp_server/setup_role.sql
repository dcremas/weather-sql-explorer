-- mcp_ro — the read-only role the MCP server connects as.
--
-- Follows the existing *_ro convention on this box (api_ro, prosite_ro,
-- superset_ro, weblog_reader). The point is defence in depth: the server code
-- already never builds SQL from model-supplied strings, but a role that is
-- physically incapable of writing means a bug in that code cannot damage the
-- warehouse either.
--
-- Apply through the SSH tunnel as a role with CREATEROLE:
--   psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d weatherdata -f setup_role.sql
--
-- Idempotent: safe to re-run.

\set ON_ERROR_STOP on

-- 1. The role itself ---------------------------------------------------------
-- NOINHERIT so it cannot pick up privileges from any role it is later added to.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_ro') THEN
        CREATE ROLE mcp_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
        RAISE NOTICE 'created role mcp_ro — set a password next (see README)';
    ELSE
        RAISE NOTICE 'role mcp_ro already exists — leaving password untouched';
    END IF;
END
$$;

-- 2. Belt and braces: every session this role opens is read-only, and no query
--    it issues can run away with the warehouse's CPU. Both are role-level
--    defaults, so they apply even if the client forgets to ask for them.
ALTER ROLE mcp_ro SET default_transaction_read_only = on;
ALTER ROLE mcp_ro SET statement_timeout = '30s';
ALTER ROLE mcp_ro SET idle_in_transaction_session_timeout = '60s';

-- 3. weatherdata — the GHCNh warehouse ---------------------------------------
GRANT CONNECT ON DATABASE weatherdata TO mcp_ro;
GRANT USAGE   ON SCHEMA public        TO mcp_ro;

-- Named explicitly rather than ALL TABLES: a table added later should have to
-- be granted deliberately, not inherited into the model's reach by accident.
GRANT SELECT ON
    public.observations,
    public.locations,
    public.loc_subset,
    public.regions
TO mcp_ro;

-- obs_baro_impact is owned by ghcnh_etl, so this grant only succeeds when run
-- by that role or a superuser. It is NOT optional any more — run_sql exposes all
-- ten tables, so a missing grant here shows up as a permission error mid-answer.
--
-- The durable half of this lives in sql/analytics_slp_decrease.sql, which
-- DROP/CREATEs this table on every load and therefore has to re-issue the grant.
-- Granting it here only fixes today; that file is what keeps it granted.
--
-- Note the check afterwards. A GRANT on a table you do not own raises a
-- *warning*, not an exception ("no privileges were granted for ..."), so an
-- EXCEPTION handler never fires and the block would cheerfully report success
-- on a grant that did nothing. Asking has_table_privilege is the only honest
-- way to find out what actually happened.
DO $$
BEGIN
    GRANT SELECT ON public.obs_baro_impact TO mcp_ro;

    IF has_table_privilege('mcp_ro', 'public.obs_baro_impact', 'SELECT') THEN
        RAISE NOTICE 'obs_baro_impact: SELECT granted';
    ELSE
        RAISE WARNING 'obs_baro_impact: NOT granted (owned by ghcnh_etl). '
                      'run_sql CAN reach this table, so leaving it ungranted '
                      'means those questions fail at query time. Re-run this '
                      'file as ghcnh_etl or postgres.';
    END IF;
END
$$;

-- Explicitly deny the future: no default privileges are granted, so tables
-- created later are invisible to mcp_ro until someone grants them.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM mcp_ro;

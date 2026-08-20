-- mcp_ro grants for the apple_weatherkit database.
--
-- Separate file because GRANT USAGE ON SCHEMA and GRANT SELECT ON TABLE are
-- per-database: they apply to whichever database the session is connected to,
-- so they cannot be folded into setup_role.sql (which runs against weatherdata).
-- Run setup_role.sql FIRST — it creates the role this file grants to.
--
--   psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d apple_weatherkit \
--        -f setup_role_apple_weatherkit.sql
--
-- Idempotent: safe to re-run.

\set ON_ERROR_STOP on

GRANT CONNECT ON DATABASE apple_weatherkit TO mcp_ro;
GRANT USAGE   ON SCHEMA public             TO mcp_ro;

-- All five tables. The typed forecast tools only need hourlyforecasts (readings)
-- and locations (station names), but run_sql exposes the whole database, so the
-- remaining three are granted too:
--   hf_baro_impact  pressure deltas over the forecast window, the forward-looking
--                   twin of weatherdata.obs_baro_impact
--   regions         state -> region/sub_region, plus tz_abbreviation, which is the
--                   only join path from a station to time_zones
--   time_zones      abbreviation -> utc_offset; hourlyforecasts.time is naive local
--                   time, so this is what makes it comparable across stations.
--                   CAVEAT, verified 2026-08-20: all 51 regions rows join (no
--                   orphans), but they only ever carry STANDARD abbreviations --
--                   EST/CST/MST/PST/AKST/HST, never the *DT variants that also
--                   exist in time_zones. So this offset is the standard-time one
--                   year-round and is an hour off during DST. Fine for grouping
--                   stations by zone; wrong for exact local->UTC conversion.
GRANT SELECT ON
    public.hourlyforecasts,
    public.locations,
    public.hf_baro_impact,
    public.regions,
    public.time_zones
TO mcp_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM mcp_ro;

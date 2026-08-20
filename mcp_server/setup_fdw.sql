-- postgres_fdw: expose the apple_weatherkit tables inside weatherdata as schema `awk`.
--
-- WHY THIS EXISTS
-- weatherdata (9.1M historical observations) and apple_weatherkit (the forward
-- WeatherKit forecast) are separate databases, and Postgres cannot join across
-- databases in a single query. The station ids are identical in both, so the most
-- natural question a visitor asks -- "how does tomorrow's forecast compare to this
-- station's own history?" -- is exactly the one that was impossible to answer in
-- one statement. Without this, the text-to-SQL app would have to issue two queries
-- and stitch them in Python, which the model cannot express as SQL and therefore
-- cannot be asked to do.
--
-- After this runs, `awk.hourlyforecasts` joins to `public.observations` directly.
--
-- MUST BE RUN AS A SUPERUSER, on the box (CREATE EXTENSION and CREATE SERVER both
-- require it; dustincremascoli is NOT a superuser and is not a member of one):
--
--   ssh ec2-user@<EC2_PUBLIC_IP>
--   sudo -u postgres psql -d weatherdata -v mcp_password="'<the mcp_ro password>'" \
--        -f setup_fdw.sql
--
-- The password is passed in rather than written here so this file stays committable.
-- Idempotent: safe to re-run. Re-running re-imports the foreign tables, which picks
-- up any column added upstream.

\set ON_ERROR_STOP on

-- 1. The wrapper --------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

-- 2. The foreign server -------------------------------------------------------
-- Loopback: 127.0.0.1:5432 is this same postmaster, which pg_hba admits with
-- `host all all 127.0.0.1/32 scram-sha-256`. Public 5432 is closed at the security
-- group and firewalld, but this connection never leaves the box, so neither cares.
--
-- Two options are doing real work here:
--   updatable 'false'  a FOURTH read-only layer, below the role, the transaction
--                      and the SQL parser. Even a superuser cannot write through
--                      this server without altering it first.
--   fetch_size 10000   the default is 100 rows per round trip. On a 53K-row
--                      forecast scan that is 537 round trips instead of 6.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = 'awk_fdw') THEN
        CREATE SERVER awk_fdw
            FOREIGN DATA WRAPPER postgres_fdw
            OPTIONS (host '127.0.0.1', port '5432', dbname 'apple_weatherkit',
                     updatable 'false', fetch_size '10000');
        RAISE NOTICE 'created foreign server awk_fdw';
    ELSE
        RAISE NOTICE 'foreign server awk_fdw already exists';
    END IF;
END
$$;

-- 3. The user mapping ---------------------------------------------------------
-- postgres_fdw refuses to connect as a non-superuser without a password (the
-- `password_required` default), so mcp_ro's password has to be stored here. That
-- means the password now lives in TWO places: mcp_server/.env and pg_user_mapping.
-- ROTATING IT REQUIRES CHANGING BOTH -- change only the role and every foreign-table
-- query starts failing with an authentication error while the local queries keep
-- working, which is a confusing way to find out.
--
-- pg_user_mapping is readable only by superusers, so the password is not exposed to
-- mcp_ro itself or to any other role.
DROP USER MAPPING IF EXISTS FOR mcp_ro SERVER awk_fdw;
CREATE USER MAPPING FOR mcp_ro SERVER awk_fdw
    OPTIONS (user 'mcp_ro', password :mcp_password);

GRANT USAGE ON FOREIGN SERVER awk_fdw TO mcp_ro;

-- A mapping for the superuser running this file is ALSO required, and it is easy
-- to miss: IMPORT FOREIGN SCHEMA below connects to the remote database as the
-- CURRENT user to read its catalog, so without this the import fails with
-- "user mapping not found for postgres" after everything else has succeeded.
--
-- It deliberately maps postgres to the REMOTE mcp_ro rather than to a remote
-- postgres. Two reasons: the remote postgres role has no password and pg_hba
-- demands scram over 127.0.0.1 even for loopback, so a postgres->postgres mapping
-- would need a password invented for it; and reading the catalog as a SELECT-only
-- role means this admin path cannot write to apple_weatherkit either.
DROP USER MAPPING IF EXISTS FOR postgres SERVER awk_fdw;
CREATE USER MAPPING FOR postgres SERVER awk_fdw
    OPTIONS (user 'mcp_ro', password :mcp_password);

-- 4. The foreign tables -------------------------------------------------------
-- A separate schema, not public, so it is obvious in every query which side of the
-- join is the forecast database. `awk.` is a visible marker that the row came over
-- the wrapper.
CREATE SCHEMA IF NOT EXISTS awk;

-- Dropped first so the IMPORT below is re-runnable. IMPORT FOREIGN SCHEMA has no
-- OR REPLACE and errors with "relation already exists" on a second run, which
-- would make this file a one-shot. Foreign tables hold no data of their own, so
-- dropping them costs nothing -- but CASCADE would take any local view built on
-- them with it, so views over `awk.` need recreating after a re-run.
DROP FOREIGN TABLE IF EXISTS
    awk.hourlyforecasts, awk.hf_baro_impact, awk.locations,
    awk.regions, awk.time_zones CASCADE;

-- LIMIT TO rather than importing everything, for the same reason the grants name
-- tables explicitly: a table added to apple_weatherkit later should have to be
-- exposed deliberately.
IMPORT FOREIGN SCHEMA public
    LIMIT TO (hourlyforecasts, hf_baro_impact, locations, regions, time_zones)
    FROM SERVER awk_fdw INTO awk;

GRANT USAGE ON SCHEMA awk TO mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA awk TO mcp_ro;

-- 5. Guidance for the model ---------------------------------------------------
-- These comments are not decoration: describe_table surfaces them, so they land in
-- the model's context. The duplicate-table warning matters most -- `locations` and
-- `regions` exist on BOTH sides with nearly identical contents (28,350 vs 28,345
-- rows), and a model that joins public.observations to awk.locations pays a network
-- round trip for data sitting in the local database.
--
-- The fan-out warning below is not hypothetical. The obvious phrasing --
-- `FROM observations o JOIN awk.hourlyforecasts f ON f.station = o.station`
-- then GROUP BY -- was written during this build, and it times out: `station` is
-- not unique on either side, so it pairs 9.17M observation rows with ~480
-- forecast rows each. A model writes that version first, so the comment tells it
-- the CTE shape explicitly rather than just saying "join on station".
COMMENT ON FOREIGN TABLE awk.hourlyforecasts IS
    'Apple WeatherKit hourly forecast, rolling ~2-week forward window, same 112 '
    'stations as public.observations. `time` is naive LOCAL time, not UTC. '
    'Units: temp_f/feelslike_f F, wind_mph/gust_mph mph, pressure_in inHg, '
    'precip_in in, vis_miles miles, humidity/cloud percent -- the same units as '
    'public.observations, so forecast-vs-history needs NO conversion. '
    'PERFORMANCE, IMPORTANT: do NOT write '
    '`FROM observations o JOIN awk.hourlyforecasts f ON f.station = o.station` '
    'and then aggregate. Station is not unique on either side, so that pairs each '
    'of 9.17M observation rows with ~480 forecast rows for its station -- about '
    '4.4 billion intermediate rows, and it exceeds the 30s timeout. Instead '
    'aggregate each side to one row per station in a CTE FIRST, then join the two '
    'CTEs on station: '
    'WITH fc AS (SELECT station, avg(temp_f) FROM awk.hourlyforecasts GROUP BY station), '
    'hist AS (SELECT station, avg(tmp) FROM public.observations GROUP BY station) '
    'SELECT * FROM hist JOIN fc USING (station).';
COMMENT ON FOREIGN TABLE awk.hf_baro_impact IS
    'Pressure deltas (3/6/24h) over the forecast window. The forward-looking twin '
    'of public.obs_baro_impact, same columns plus utc_offset.';
COMMENT ON FOREIGN TABLE awk.locations IS
    'DUPLICATE -- prefer public.locations, which is local and near-identical. '
    'Only reach for this to prove the two station rosters agree.';
COMMENT ON FOREIGN TABLE awk.regions IS
    'DUPLICATE of public.regions except it adds tz_abbreviation, the only join path '
    'to awk.time_zones. Use public.regions unless you need the time zone.';
COMMENT ON FOREIGN TABLE awk.time_zones IS
    'Time-zone abbreviation -> utc_offset. CAVEAT: regions.tz_abbreviation only ever '
    'holds STANDARD abbreviations (EST/CST/MST/PST/AKST/HST), never the *DT ones, so '
    'the offset is standard time year-round and is an hour off during DST. Fine for '
    'grouping stations by zone; wrong for exact local->UTC conversion.';

-- 6. Prove it ------------------------------------------------------------------
-- A real cross-database join. If this returns rows, the whole chain works:
-- extension, server, mapping, import, grants.
SELECT count(*) AS joined_rows,
       count(DISTINCT f.station) AS stations
FROM awk.hourlyforecasts f
JOIN public.loc_subset s ON s.station = f.station;

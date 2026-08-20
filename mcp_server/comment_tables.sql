-- Table and column COMMENTs on the weatherdata public tables.
--
-- WHY BOTHER
-- describe_table and list_schema read these straight out of pg_catalog and hand
-- them to the model, so this file is effectively the schema documentation the
-- text-to-SQL prompt is built from. It is the cheapest accuracy lever available:
-- the column names alone are actively misleading.
--
-- `tmp`, `dew`, `slp`, `wnd`, `vis`, `prp`, `cig` are all unlabelled. Nothing in
-- the schema says `tmp` is already Fahrenheit -- and on 2026-08-20 that assumption
-- cost a real wrong answer during this build: a forecast-vs-history query applied
-- C-to-F to `tmp` and reported August normals of 151 F for Los Angeles. It looked
-- like a plausible query and returned plausible-shaped nonsense. A model asked
-- the same question makes the same mistake, and nobody is checking its arithmetic.
--
-- Units verified 2026-08-20 by range against 9.17M live rows, not from docs:
--   tmp -50.8..118.9  dew -50.8..91.2   slp 28.4..32.8   wnd 0..109.4
--   vis 0..99.4       prp 0..10.6
-- Those ranges are only consistent with F / F / inHg / mph / statute miles / in.
--
-- Apply as a role that owns the tables (COMMENT requires ownership -- there is no
-- grantable "comment" privilege). obs_baro_impact is owned by ghcnh_etl, so that
-- one needs ghcnh_etl or a superuser:
--   sudo -u postgres psql -d weatherdata -f comment_tables.sql
--
-- Idempotent: COMMENT ON always overwrites.

\set ON_ERROR_STOP on

-- observations ---------------------------------------------------------------
COMMENT ON TABLE public.observations IS
    'The historical warehouse: 9.17M hourly surface observations from 112 US '
    'stations, 2019-01-01 to present, sourced from NOAA GHCNh. One row per '
    'station per hour per report. THIS IS THE MAIN FACT TABLE -- join it to '
    'loc_subset or locations on `station` to get names and coordinates. For '
    'forecast comparisons join awk.hourlyforecasts on `station`; both sides are '
    'already in Fahrenheit, so no unit conversion is needed.';

COMMENT ON COLUMN public.observations.station IS
    '11-character GHCNh station id, e.g. USW00023174. The join key to '
    'locations, loc_subset, obs_baro_impact and awk.hourlyforecasts.';
COMMENT ON COLUMN public.observations.date IS
    'Observation timestamp (no time zone). Local standard time at the station.';
COMMENT ON COLUMN public.observations.tmp IS
    'Air temperature in DEGREES FAHRENHEIT -- already F, do NOT convert from '
    'Celsius. Observed range -50.8 to 118.9.';
COMMENT ON COLUMN public.observations.dew IS
    'Dew point in DEGREES FAHRENHEIT. Already F, do not convert.';
COMMENT ON COLUMN public.observations.slp IS
    'Sea-level pressure in INCHES OF MERCURY (inHg), roughly 28.4 to 32.8. '
    'Not hPa/millibars -- a value near 1013 would be hPa and does not occur here.';
COMMENT ON COLUMN public.observations.wnd IS 'Wind speed in MILES PER HOUR.';
COMMENT ON COLUMN public.observations.vis IS 'Visibility in STATUTE MILES.';
COMMENT ON COLUMN public.observations.prp IS 'Precipitation in INCHES.';
COMMENT ON COLUMN public.observations.cig IS 'Cloud ceiling height in FEET.';
COMMENT ON COLUMN public.observations.source IS
    'GHCNh source code, 3 digits (223, 343, 413). NOTE: older ISD data used '
    'single digits (6, 7). Filtering on single-digit codes excludes every GHCNh '
    'row and silently returns nothing -- do not filter on this column.';
COMMENT ON COLUMN public.observations.report_type IS
    'GHCNh report type, e.g. FM-15 (routine hourly), SAO. Mostly not useful to '
    'filter on.';

-- locations ------------------------------------------------------------------
COMMENT ON TABLE public.locations IS
    'Full station reference from the source feed: 28,350 stations worldwide, of '
    'which only the 112 in loc_subset actually have observations. PREFER '
    'loc_subset when you want stations that have data -- joining observations to '
    'this table works but the extra 28k rows are stations with nothing to show.';
COMMENT ON COLUMN public.locations.station IS '11-character station id; join key.';
COMMENT ON COLUMN public.locations.elevation IS 'Station elevation in METRES.';
COMMENT ON COLUMN public.locations.begin IS 'First date the station reported.';
COMMENT ON COLUMN public.locations."end" IS 'Last date the station reported.';

-- loc_subset -----------------------------------------------------------------
COMMENT ON TABLE public.loc_subset IS
    'The curated 112-station roster, as one row per station per YEAR (896 rows, '
    'not 112) -- so joining it to observations without restricting `year` '
    'multiplies your row count by the number of years. Use '
    '`SELECT DISTINCT station, station_name, state, region FROM loc_subset` when '
    'you just want station names. Carries region/sub_region, which is the easiest '
    'way to group stations geographically.';
COMMENT ON COLUMN public.loc_subset.year IS
    'Calendar year this station-year scaffold row represents. The reason this '
    'table has 896 rows rather than 112.';
COMMENT ON COLUMN public.loc_subset.station_name IS
    'Human-readable airport/station name, e.g. LOS ANGELES INTERNATIONAL AIRPORT. '
    'Match place names against this with ILIKE.';

-- regions --------------------------------------------------------------------
COMMENT ON TABLE public.regions IS
    'US state -> census region / sub_region lookup, 51 rows. Join on `state` to '
    'roll stations up geographically.';

-- obs_baro_impact ------------------------------------------------------------
COMMENT ON TABLE public.obs_baro_impact IS
    'Derived: per-station sea-level-pressure change over 3, 6 and 24 hours, '
    '658,650 rows, for the subset of stations the barometric visualisation plots '
    '(NOT all 112). Rebuilt from observations on every load, so it is a '
    'convenience table -- the same numbers can be computed from observations with '
    'window functions. Deltas are in INCHES OF MERCURY; negative means falling '
    'pressure, which is the storm-approach signal.';
COMMENT ON COLUMN public.obs_baro_impact.slp_3hr_diff IS
    'Sea-level pressure change over the previous 3 hours, in inHg. Negative = '
    'falling.';
COMMENT ON COLUMN public.obs_baro_impact.slp_6hr_diff IS
    'Sea-level pressure change over the previous 6 hours, in inHg.';
COMMENT ON COLUMN public.obs_baro_impact.slp_24hr_diff IS
    'Sea-level pressure change over the previous 24 hours, in inHg.';

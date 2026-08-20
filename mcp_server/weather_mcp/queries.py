"""SQL for the weather MCP server.

**No query here is built from caller-supplied text.** Every value a tool accepts
travels as a bound parameter. The only places an identifier is substituted into
SQL are the measure column, the aggregate function, and the date_trunc unit —
and each of those is looked up in a fixed dictionary first, so an unrecognised
value raises before it can reach a string format.

CHANGED 2026-08-20 — there IS now a `run_sql` tool. This docstring used to end
"that is the whole reason this server exposes typed tools instead of a `run_sql`
tool: a SQL passthrough would put the model's output directly into the query
planner, and no amount of prompting makes that safe." The observation about
prompting still holds. What changed is the conclusion: model-generated SQL is
gated by guard.py (single read-only statement, parsed on the AST, table
allow-list, forced LIMIT) and executed by a role that holds SELECT on ten tables
and can do nothing else. The safety comes from the role, not from the absence of
the tool — see guard.py's module docstring for the full argument.

Everything in THIS file is still fixed, parameterised SQL. run_sql's statement
never passes through here; it goes through guard.check() and db.query_guarded().

Units, which the raw column names do not reveal (verified against live data
ranges on 2026-08-20):

    tmp  °F          dew  °F          slp  inHg
    wnd  mph         vis  statute mi  prp  inches       cig  ft
"""

from __future__ import annotations

# --- allow-lists -------------------------------------------------------------
# Keys are what a tool accepts; values are what reaches SQL. A caller can only
# ever select a key, so the SQL side is closed.

MEASURES: dict[str, str] = {
    "temperature": "tmp",
    "dew_point": "dew",
    "sea_level_pressure": "slp",
    "wind_speed": "wnd",
    "visibility": "vis",
    "precipitation": "prp",
    "ceiling_height": "cig",
}

MEASURE_UNITS: dict[str, str] = {
    "temperature": "F",
    "dew_point": "F",
    "sea_level_pressure": "inHg",
    "wind_speed": "mph",
    "visibility": "miles",
    "precipitation": "inches",
    "ceiling_height": "feet",
}

AGGREGATES: dict[str, str] = {
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "sum": "SUM",
}

GRANULARITIES: dict[str, str] = {
    "hour": "hour",
    "day": "day",
    "month": "month",
    "year": "year",
}


def resolve_measure(name: str) -> str:
    try:
        return MEASURES[name]
    except KeyError:
        raise ValueError(
            f"unknown measure {name!r}; choose one of {sorted(MEASURES)}"
        ) from None


def resolve_aggregate(name: str) -> str:
    try:
        return AGGREGATES[name]
    except KeyError:
        raise ValueError(
            f"unknown aggregate {name!r}; choose one of {sorted(AGGREGATES)}"
        ) from None


def resolve_granularity(name: str) -> str:
    try:
        return GRANULARITIES[name]
    except KeyError:
        raise ValueError(
            f"unknown granularity {name!r}; choose one of {sorted(GRANULARITIES)}"
        ) from None


# --- station discovery -------------------------------------------------------
# loc_subset is the curated 112-station roster (one row per station-year), which
# is why it is the join target rather than `locations` (28,350 rows, most of
# them stations this warehouse never ingests).

LIST_STATIONS = """
SELECT DISTINCT ON (s.station)
       s.station,
       s.station_name,
       s.state,
       s.region,
       s.sub_region,
       s.lat,
       s.lon
FROM loc_subset s
WHERE (%(state)s::text      IS NULL OR UPPER(s.state)      = UPPER(%(state)s))
  AND (%(region)s::text     IS NULL OR UPPER(s.region)     = UPPER(%(region)s))
  AND (%(sub_region)s::text IS NULL OR UPPER(s.sub_region) = UPPER(%(sub_region)s))
  AND (%(name_contains)s::text IS NULL
       OR s.station_name ILIKE '%%' || %(name_contains)s::text || '%%'
       OR s.station       LIKE '%%' || %(name_contains)s::text || '%%')
ORDER BY s.station, s.year DESC
LIMIT %(limit)s
"""

# --- coverage / health -------------------------------------------------------

COVERAGE = """
SELECT COUNT(*)                    AS row_count,
       COUNT(DISTINCT station)     AS station_count,
       MIN(date)                   AS earliest,
       MAX(date)                   AS latest,
       NOW() - MAX(date)           AS behind_now
FROM observations
"""

COVERAGE_BY_YEAR = """
SELECT EXTRACT(YEAR FROM date)::int AS year,
       COUNT(*)                     AS row_count,
       COUNT(DISTINCT station)      AS station_count,
       MIN(date)                    AS earliest,
       MAX(date)                    AS latest
FROM observations
GROUP BY 1
ORDER BY 1
"""

# --- raw observations --------------------------------------------------------

GET_OBSERVATIONS = """
SELECT o.station,
       s.station_name,
       o.date,
       o.tmp AS temperature_f,
       o.dew AS dew_point_f,
       o.slp AS sea_level_pressure_inhg,
       o.wnd AS wind_speed_mph,
       o.vis AS visibility_mi,
       o.prp AS precipitation_in
FROM observations o
LEFT JOIN LATERAL (
    SELECT station_name FROM loc_subset
    WHERE station = o.station ORDER BY year DESC LIMIT 1
) s ON TRUE
WHERE o.station = %(station)s
  AND o.date >= %(start)s
  AND o.date <  %(end)s
ORDER BY o.date DESC
LIMIT %(limit)s
"""

LATEST_PER_STATION = """
SELECT DISTINCT ON (o.station)
       o.station,
       s.station_name,
       s.state,
       o.date,
       o.tmp AS temperature_f,
       o.dew AS dew_point_f,
       o.slp AS sea_level_pressure_inhg,
       o.wnd AS wind_speed_mph
FROM observations o
LEFT JOIN LATERAL (
    SELECT station_name, state FROM loc_subset
    WHERE station = o.station ORDER BY year DESC LIMIT 1
) s ON TRUE
WHERE (%(station)s::text IS NULL OR o.station = %(station)s::text)
ORDER BY o.station, o.date DESC
LIMIT %(limit)s
"""


def summarize(measure_col: str, unit: str) -> str:
    """Per-period min/avg/max for one station and measure.

    `measure_col` and `unit` come from resolve_measure()/MEASURE_UNITS, never
    from a caller. date_trunc's unit is bound as a parameter, so it needs no
    allow-list of its own — but resolve_granularity() checks it anyway so a
    typo fails with a useful message instead of a Postgres error.
    """
    return f"""
SELECT date_trunc(%(granularity)s, date) AS period,
       COUNT({measure_col})              AS readings,
       ROUND(AVG({measure_col})::numeric, 2) AS avg_{unit},
       ROUND(MIN({measure_col})::numeric, 2) AS min_{unit},
       ROUND(MAX({measure_col})::numeric, 2) AS max_{unit}
FROM observations
WHERE station = %(station)s
  AND date >= %(start)s
  AND date <  %(end)s
  AND {measure_col} IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC
LIMIT %(limit)s
"""


DIRECTIONS: dict[str, str] = {"highest": "DESC", "lowest": "ASC"}


def resolve_direction(name: str) -> str:
    try:
        return DIRECTIONS[name]
    except KeyError:
        raise ValueError(
            f"unknown order {name!r}; choose one of {sorted(DIRECTIONS)}"
        ) from None


def rank_stations(measure_col: str, agg_fn: str, direction: str) -> str:
    """Rank the roster by one aggregated measure over a window.

    All three arguments are outputs of the resolve_* helpers above, so the only
    strings that can reach this f-string are dictionary values defined in this
    module. `direction` is resolved here too rather than left as a `.format()`
    placeholder — a half-formatted SQL template is exactly the kind of thing
    that later grows a caller-supplied value.
    """
    if direction not in DIRECTIONS.values():
        raise ValueError(f"direction not allow-listed: {direction!r}")
    return f"""
SELECT o.station,
       s.station_name,
       s.state,
       s.region,
       ROUND({agg_fn}(o.{measure_col})::numeric, 2) AS value,
       COUNT(o.{measure_col})                       AS readings
FROM observations o
JOIN LATERAL (
    SELECT station_name, state, region FROM loc_subset
    WHERE station = o.station ORDER BY year DESC LIMIT 1
) s ON TRUE
WHERE o.date >= %(start)s
  AND o.date <  %(end)s
  AND o.{measure_col} IS NOT NULL
  AND (%(region)s::text IS NULL OR UPPER(s.region) = UPPER(%(region)s::text))
  AND (%(state)s::text  IS NULL OR UPPER(s.state)  = UPPER(%(state)s::text))
GROUP BY o.station, s.station_name, s.state, s.region
HAVING COUNT(o.{measure_col}) >= %(min_readings)s
ORDER BY value {direction}
LIMIT %(limit)s
"""


def compare_stations(measure_col: str, unit: str) -> str:
    return f"""
SELECT o.station,
       s.station_name,
       s.state,
       COUNT(o.{measure_col})                       AS readings,
       ROUND(AVG(o.{measure_col})::numeric, 2)      AS avg_{unit},
       ROUND(MIN(o.{measure_col})::numeric, 2)      AS min_{unit},
       ROUND(MAX(o.{measure_col})::numeric, 2)      AS max_{unit},
       ROUND(STDDEV_SAMP(o.{measure_col})::numeric, 2) AS stddev_{unit}
FROM observations o
JOIN LATERAL (
    SELECT station_name, state FROM loc_subset
    WHERE station = o.station ORDER BY year DESC LIMIT 1
) s ON TRUE
WHERE o.station = ANY(%(stations)s)
  AND o.date >= %(start)s
  AND o.date <  %(end)s
  AND o.{measure_col} IS NOT NULL
GROUP BY o.station, s.station_name, s.state
ORDER BY avg_{unit} DESC NULLS LAST
"""


# --- the anomaly query -------------------------------------------------------
# The baseline deliberately EXCLUDES the target year. Including it would blend
# the year being measured into the mean it is measured against, shrinking every
# anomaly toward zero — subtly, and worse the shorter the baseline.

ANOMALY = """
WITH target AS (
    SELECT station,
           AVG(tmp)   AS mean_f,
           COUNT(tmp) AS readings
    FROM observations
    WHERE EXTRACT(YEAR  FROM date) = %(year)s
      AND EXTRACT(MONTH FROM date) = %(month)s
      AND tmp IS NOT NULL
    GROUP BY station
),
baseline AS (
    SELECT station,
           AVG(tmp)                          AS mean_f,
           COUNT(tmp)                        AS readings,
           COUNT(DISTINCT EXTRACT(YEAR FROM date)) AS years
    FROM observations
    WHERE EXTRACT(MONTH FROM date) = %(month)s
      AND EXTRACT(YEAR  FROM date) BETWEEN %(baseline_start)s AND %(baseline_end)s
      AND EXTRACT(YEAR  FROM date) <> %(year)s
      AND tmp IS NOT NULL
    GROUP BY station
)
SELECT t.station,
       s.station_name,
       s.state,
       s.region,
       ROUND(t.mean_f::numeric, 2)               AS target_mean_f,
       ROUND(b.mean_f::numeric, 2)               AS baseline_mean_f,
       ROUND((t.mean_f - b.mean_f)::numeric, 2)  AS anomaly_f,
       t.readings                                AS target_readings,
       b.years                                   AS baseline_years
FROM target t
JOIN baseline b ON b.station = t.station
JOIN LATERAL (
    SELECT station_name, state, region FROM loc_subset
    WHERE station = t.station ORDER BY year DESC LIMIT 1
) s ON TRUE
WHERE t.readings >= %(min_readings)s
  AND b.years    >= %(min_baseline_years)s
  AND (%(region)s::text IS NULL OR UPPER(s.region) = UPPER(%(region)s::text))
ORDER BY anomaly_f DESC
LIMIT %(limit)s
"""

# --- forecasts (apple_weatherkit) -------------------------------------------

GET_FORECAST = """
SELECT station,
       time,
       temp_f,
       feelslike_f,
       wind_mph,
       gust_mph,
       pressure_in,
       precip_in,
       humidity,
       cloud,
       vis_miles
FROM hourlyforecasts
WHERE station = %(station)s
  AND time >= %(start)s
ORDER BY time
LIMIT %(limit)s
"""

FORECAST_WINDOW = """
SELECT station, MIN(time) AS earliest, MAX(time) AS latest, COUNT(*) AS rows
FROM hourlyforecasts
WHERE (%(station)s::text IS NULL OR station = %(station)s::text)
GROUP BY station
ORDER BY station
LIMIT %(limit)s
"""


# --- introspection -----------------------------------------------------------
# What list_schema and describe_table read. These deliberately use pg_catalog
# rather than information_schema for two reasons: information_schema does not
# expose COMMENTs (obj_description / col_description have no equivalent there),
# and it does not report foreign tables consistently across versions — the whole
# `awk` schema would be invisible.
#
# Both are safe against a hostile caller for the same reason as every other query
# here: the schema/table names arrive as bound parameters, never interpolated.
#
# reltuples is the planner's ESTIMATE, not a count. That is deliberate: an exact
# count(*) on observations reads 9.17M rows and is not worth 2 seconds when the
# model only needs to know the table is large.
#
# It reads -1 when there are no statistics -- both for a table never analysed and
# (verified 2026-08-20) for every awk.* foreign table, since the local planner
# keeps no statistics for those unless ANALYZE is run on them explicitly. -1 means
# "unknown", NOT "empty"; the tools label the field as an estimate so a model does
# not read a foreign table as having no rows.

LIST_SCHEMA = """
SELECT n.nspname                                   AS schema,
       c.relname                                   AS table_name,
       CASE c.relkind WHEN 'r' THEN 'table'
                      WHEN 'f' THEN 'foreign table'
                      WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized view' END AS kind,
       c.reltuples::bigint                         AS estimated_rows,
       obj_description(c.oid, 'pg_class')          AS description,
       (SELECT count(*) FROM pg_attribute a
         WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
                                                   AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%(schemas)s)
  AND c.relkind IN ('r', 'f', 'v', 'm')
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY n.nspname, c.relname
"""

DESCRIBE_TABLE = """
SELECT a.attname                                        AS column_name,
       format_type(a.atttypid, a.atttypmod)             AS data_type,
       NOT a.attnotnull                                 AS nullable,
       col_description(c.oid, a.attnum)                 AS description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = %(schema)s
  AND c.relname = %(table)s
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY a.attnum
"""

DESCRIBE_TABLE_META = """
SELECT n.nspname                          AS schema,
       c.relname                          AS table_name,
       CASE c.relkind WHEN 'r' THEN 'table'
                      WHEN 'f' THEN 'foreign table'
                      WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized view' END AS kind,
       c.reltuples::bigint                AS estimated_rows,
       obj_description(c.oid, 'pg_class') AS description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s
  AND c.relname = %(table)s
  AND c.relkind IN ('r', 'f', 'v', 'm')
  AND has_table_privilege(c.oid, 'SELECT')
"""

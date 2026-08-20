"""Read-only MCP server over the weather warehouse.

Thirteen tools covering schema discovery, station discovery, historical
observations, climate anomalies, WeatherKit forecasts, and open-ended SQL.

Two kinds of tool, and the distinction matters when reading this file:

  * TEN TYPED TOOLS (list_stations ... forecast_coverage). Fixed, parameterised
    SQL from queries.py. Fast paths for the questions worth answering the same
    way every time.
  * THREE OPEN TOOLS (list_schema, describe_table, run_sql), added 2026-08-20 to
    support text-to-SQL. run_sql executes model-generated SQL, which this server
    previously refused to do on principle — guard.py explains what changed and
    why the role, not the absence of the tool, is what makes it safe.

Run directly for stdio (what Claude Desktop launches):

    ./.venv/bin/python -m weather_mcp.server

Smoke-test without an MCP client:

    ./.venv/bin/python -m weather_mcp.selftest
"""

from __future__ import annotations

import atexit
import datetime as dt
import os
import sys

import psycopg
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import db, guard, queries

# MCP Python SDK v2: MCPServer replaces the v1 `FastMCP` class (which lived at
# mcp.server.fastmcp and no longer exists). The decorator and run() shapes are
# unchanged, so this is a one-line swap if you are porting an older server.
mcp = MCPServer(
    "weather-warehouse",
    version=__import__("weather_mcp").__version__,
    instructions=(
        "Read-only access to a GHCNh weather warehouse: 9.1M hourly observations "
        "from 112 US stations, 2019 to present, plus Apple WeatherKit hourly "
        "forecasts for the same stations. Start with list_stations to turn a place "
        "name into a station id, then use the observation or analysis tools. "
        "Call data_coverage first if a recent-period query returns less than "
        "expected — the upstream feed has regressed before.\n\n"
        "For anything the typed tools do not cover, use run_sql: call "
        "list_schema once to see the ten readable tables, describe_table for "
        "columns and units, then run_sql with a single read-only SELECT. "
        "Forecast tables are exposed as awk.* inside the same database, so one "
        "query can join forecast against history on `station`."
    ),
)
atexit.register(db.close_all)

# Caps. The model can ask for fewer, never more — a tool that can return 9M rows
# is a tool that will eventually be asked to.
MAX_ROWS = 500
DEFAULT_ROWS = 50


def _clamp(limit: int | None, default: int = DEFAULT_ROWS) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_ROWS))


def _parse_date(value: str, field: str) -> dt.datetime:
    """Accept YYYY-MM-DD or a full ISO timestamp; reject anything else.

    Parsing here rather than passing the string through means a malformed date
    fails with a clear message instead of a Postgres cast error.
    """
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{field} must be ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS), got {value!r}"
        ) from None


def _jsonable(rows: list[dict]) -> list[dict]:
    """Make datetimes, Decimals and timedeltas survive JSON serialisation."""
    out = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, (dt.datetime, dt.date)):
                clean[key] = value.isoformat()
            elif isinstance(value, dt.timedelta):
                clean[key] = f"{value.total_seconds() / 3600:.1f} hours"
            elif hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
                clean[key] = float(value)
            else:
                clean[key] = value
        out.append(clean)
    return out


def _jsonable_rows(rows: list[list]) -> list[list]:
    """Same coercion as _jsonable, for positional rows rather than dicts.

    run_sql returns columns and rows separately instead of a list of dicts,
    because `SELECT a.station, b.station` is legal SQL whose two columns would
    collapse into one key in a dict.
    """
    out = []
    for row in rows:
        clean = []
        for value in row:
            if isinstance(value, (dt.datetime, dt.date)):
                clean.append(value.isoformat())
            elif isinstance(value, dt.timedelta):
                clean.append(f"{value.total_seconds() / 3600:.1f} hours")
            elif hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
                clean.append(float(value))
            else:
                clean.append(value)
        out.append(clean)
    return out


# =============================================================================
# Discovery
# =============================================================================

@mcp.tool()
def list_stations(
    state: str | None = None,
    region: str | None = None,
    sub_region: str | None = None,
    name_contains: str | None = None,
    limit: int | None = None,
) -> dict:
    """List weather stations in the warehouse roster.

    Call this first when a question names a place rather than a station id —
    every other tool takes the 11-character `station` id this returns.

    The roster is 112 US stations. Filters combine with AND; omit all of them
    to list everything. `region` is one of MIDWEST, NORTHEAST, SOUTH, WEST.
    `name_contains` matches the station name case-insensitively, and also
    matches against the station id.
    """
    rows = db.query(
        db.WEATHERDATA,
        queries.LIST_STATIONS,
        {
            "state": state,
            "region": region,
            "sub_region": sub_region,
            "name_contains": name_contains,
            "limit": _clamp(limit, 200),
        },
    )
    return {"count": len(rows), "stations": _jsonable(rows)}


@mcp.tool()
def data_coverage(by_year: bool = False) -> dict:
    """Report what the warehouse actually holds right now.

    Row count, station count, earliest and latest observation, and how far
    behind the present the newest reading is.

    Worth calling before drawing conclusions from a recent-period query: the
    GHCNh feed is republished upstream and has regressed before, so "no data
    for last week" is often a source condition rather than an empty result.
    Set `by_year` for the same breakdown per year, which is how you spot a
    partially-loaded year.
    """
    summary = _jsonable(db.query(db.WEATHERDATA, queries.COVERAGE))[0]
    result: dict[str, Any] = {"observations": summary}
    if by_year:
        result["by_year"] = _jsonable(db.query(db.WEATHERDATA, queries.COVERAGE_BY_YEAR))
    return result


# =============================================================================
# Observations
# =============================================================================

@mcp.tool()
def get_observations(
    station: str,
    start: str,
    end: str,
    limit: int | None = None,
) -> dict:
    """Fetch raw hourly observations for one station over a date range.

    Returns every measure at once (temperature, dew point, pressure, wind,
    visibility, precipitation), newest first. `start` is inclusive, `end`
    exclusive, both ISO dates.

    This is the detail view — for anything spanning more than a few days,
    `summarize_observations` returns far less data for the same question.
    Wind, visibility and pressure are ~86-90% populated in recent data; nulls
    are genuine gaps in the source, not errors.
    """
    rows = db.query(
        db.WEATHERDATA,
        queries.GET_OBSERVATIONS,
        {
            "station": station,
            "start": _parse_date(start, "start"),
            "end": _parse_date(end, "end"),
            "limit": _clamp(limit),
        },
    )
    return {
        "station": station,
        "count": len(rows),
        "truncated": len(rows) == _clamp(limit),
        "units": {"temperature": "F", "pressure": "inHg", "wind": "mph",
                  "visibility": "miles", "precipitation": "inches"},
        "observations": _jsonable(rows),
    }


@mcp.tool()
def summarize_observations(
    station: str,
    start: str,
    end: str,
    measure: Literal["temperature", "dew_point", "sea_level_pressure",
                     "wind_speed", "visibility", "precipitation",
                     "ceiling_height"] = "temperature",
    granularity: Literal["hour", "day", "month", "year"] = "day",
    limit: int | None = None,
) -> dict:
    """Aggregate one measure for one station into per-period min/avg/max.

    The workhorse for trend questions — "how did temperature move at O'Hare
    last summer" is a `granularity="day"` call, "how have summers trended" is
    `granularity="year"`. Returns newest period first.
    """
    column = queries.resolve_measure(measure)
    unit = queries.MEASURE_UNITS[measure]
    rows = db.query(
        db.WEATHERDATA,
        queries.summarize(column, unit),
        {
            "station": station,
            "start": _parse_date(start, "start"),
            "end": _parse_date(end, "end"),
            "granularity": queries.resolve_granularity(granularity),
            "limit": _clamp(limit, 200),
        },
    )
    return {
        "station": station,
        "measure": measure,
        "unit": unit,
        "granularity": granularity,
        "count": len(rows),
        "periods": _jsonable(rows),
    }


@mcp.tool()
def latest_observations(station: str | None = None, limit: int | None = None) -> dict:
    """Most recent observation for one station, or for every station.

    Omit `station` for a roster-wide snapshot — that doubles as a freshness
    check, since a station stuck behind the others is visible immediately.
    """
    rows = db.query(
        db.WEATHERDATA,
        queries.LATEST_PER_STATION,
        {"station": station, "limit": _clamp(limit, 200)},
    )
    return {"count": len(rows), "latest": _jsonable(rows)}


# =============================================================================
# Analysis
# =============================================================================

@mcp.tool()
def rank_stations(
    start: str,
    end: str,
    measure: Literal["temperature", "dew_point", "sea_level_pressure",
                     "wind_speed", "visibility", "precipitation",
                     "ceiling_height"] = "temperature",
    aggregate: Literal["avg", "min", "max", "sum"] = "avg",
    order: Literal["highest", "lowest"] = "highest",
    region: str | None = None,
    state: str | None = None,
    min_readings: int = 100,
    limit: int | None = None,
) -> dict:
    """Rank stations by an aggregated measure over a window.

    Answers "hottest / windiest / wettest station" questions. `aggregate="sum"`
    is the right choice for precipitation totals; `avg` for typical conditions;
    `max` for extremes.

    `min_readings` drops stations too sparsely reported in the window to compare
    fairly — raise it for long windows, lower it for short ones.
    """
    column = queries.resolve_measure(measure)
    rows = db.query(
        db.WEATHERDATA,
        queries.rank_stations(
            column,
            queries.resolve_aggregate(aggregate),
            queries.resolve_direction(order),
        ),
        {
            "start": _parse_date(start, "start"),
            "end": _parse_date(end, "end"),
            "region": region,
            "state": state,
            "min_readings": max(1, int(min_readings)),
            "limit": _clamp(limit, 100),
        },
    )
    return {
        "measure": measure,
        "unit": queries.MEASURE_UNITS[measure],
        "aggregate": aggregate,
        "order": order,
        "count": len(rows),
        "rankings": _jsonable(rows),
    }


@mcp.tool()
def compare_stations(
    stations: list[str],
    start: str,
    end: str,
    measure: Literal["temperature", "dew_point", "sea_level_pressure",
                     "wind_speed", "visibility", "precipitation",
                     "ceiling_height"] = "temperature",
) -> dict:
    """Compare several stations side by side over the same window.

    Returns count, mean, min, max and standard deviation per station. Pass 2-20
    station ids from `list_stations`.
    """
    if not stations:
        raise ValueError("stations must contain at least one station id")
    if len(stations) > 20:
        raise ValueError(f"at most 20 stations per call, got {len(stations)}")
    column = queries.resolve_measure(measure)
    unit = queries.MEASURE_UNITS[measure]
    rows = db.query(
        db.WEATHERDATA,
        queries.compare_stations(column, unit),
        {
            "stations": list(stations),
            "start": _parse_date(start, "start"),
            "end": _parse_date(end, "end"),
        },
    )
    return {
        "measure": measure,
        "unit": unit,
        "requested": len(stations),
        "returned": len(rows),
        "comparison": _jsonable(rows),
    }


@mcp.tool()
def temperature_anomaly(
    year: int,
    month: int,
    baseline_start_year: int = 2019,
    baseline_end_year: int = 2025,
    region: str | None = None,
    min_readings: int = 200,
    min_baseline_years: int = 3,
    limit: int | None = None,
) -> dict:
    """Rank stations by how far a given month departed from their own normal.

    For each station: mean temperature in `year`/`month`, its mean for that
    same calendar month across the baseline years, and the difference. Sorted
    warmest anomaly first, so the tail of the list is the cold end.

    Each station is compared only against itself, so a hot desert station and a
    cool coastal one are on equal footing — this measures departure from local
    normal, not absolute temperature.

    **The target year is always excluded from its own baseline**, even when it
    falls inside the baseline range; including it would pull every anomaly
    toward zero. `min_baseline_years` drops stations with too little history to
    have a meaningful normal.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    if baseline_start_year > baseline_end_year:
        raise ValueError("baseline_start_year must not exceed baseline_end_year")
    rows = db.query(
        db.WEATHERDATA,
        queries.ANOMALY,
        {
            "year": year,
            "month": month,
            "baseline_start": baseline_start_year,
            "baseline_end": baseline_end_year,
            "region": region,
            "min_readings": max(1, int(min_readings)),
            "min_baseline_years": max(1, int(min_baseline_years)),
            "limit": _clamp(limit, 200),
        },
    )
    return {
        "target": f"{year}-{month:02d}",
        "baseline": f"{baseline_start_year}-{baseline_end_year} (excluding {year})",
        "unit": "F",
        "count": len(rows),
        "note": "anomaly_f is target_mean_f minus the station's own baseline mean; "
                "positive is warmer than normal",
        "anomalies": _jsonable(rows),
    }


# =============================================================================
# Forecasts (apple_weatherkit)
# =============================================================================

@mcp.tool()
def get_forecast(station: str, start: str | None = None, limit: int | None = None) -> dict:
    """Hourly WeatherKit forecast for one station.

    Covers roughly the next two weeks and is refreshed every four hours. The
    station ids match the historical warehouse exactly (all 112 overlap), so a
    station id from `list_stations` works here unchanged.

    Defaults to forecasts from now onward; pass `start` to include past hours
    already covered by the stored forecast.
    """
    rows = db.query(
        db.APPLE_WEATHERKIT,
        queries.GET_FORECAST,
        {
            "station": station,
            "start": _parse_date(start, "start") if start else dt.datetime.now(),
            "limit": _clamp(limit, 200),
        },
    )
    return {
        "station": station,
        "count": len(rows),
        "units": {"temp_f": "F", "wind_mph": "mph", "pressure_in": "inHg",
                  "precip_in": "inches", "humidity": "%", "cloud": "%",
                  "vis_miles": "miles"},
        "forecast": _jsonable(rows),
    }


@mcp.tool()
def forecast_coverage(station: str | None = None, limit: int | None = None) -> dict:
    """Report the window the stored WeatherKit forecast currently spans.

    Use it to check forecast freshness before trusting `get_forecast` — the
    loader runs every four hours, so a window that ends in the past means the
    pipeline has stalled.
    """
    rows = db.query(
        db.APPLE_WEATHERKIT,
        queries.FORECAST_WINDOW,
        {"station": station, "limit": _clamp(limit, 200)},
    )
    return {"count": len(rows), "coverage": _jsonable(rows)}


# =============================================================================
# Schema discovery and open-ended SQL
#
# These three exist so a text-to-SQL client can work without the schema being
# hard-coded into its prompt: list_schema to see what is there, describe_table
# for columns and units, run_sql to ask. Keeping the schema in the database and
# reading it at query time means adding a column upstream does not silently make
# the prompt wrong.
# =============================================================================

# The two schemas run_sql can read. `awk` holds postgres_fdw foreign tables
# pointing at the apple_weatherkit database, which is what lets a single query
# join forecast against history.
_SCHEMAS = ["public", "awk"]


@mcp.tool()
def list_schema() -> dict:
    """List every table run_sql can read, with row estimates and descriptions.

    START HERE for any question the typed tools do not obviously cover. Returns
    ten tables across two schemas:

      public.*  the historical GHCNh warehouse (observations, station rosters)
      awk.*     Apple WeatherKit forecasts, reached over postgres_fdw

    Both schemas live in one database connection, so `public.observations` and
    `awk.hourlyforecasts` can be joined in a single SELECT — that join, on
    `station`, is how forecast-versus-history questions get answered.

    `estimated_rows` is the query planner's estimate, not a count. It reads -1 for
    every awk.* foreign table, because the local planner keeps no statistics for
    those -- that means "unknown", NOT "empty". Those tables do hold data (~53,760
    forecast rows each); count them with run_sql if you need a real number.
    """
    rows = db.query(db.WEATHERDATA, queries.LIST_SCHEMA, {"schemas": _SCHEMAS})
    return {
        "count": len(rows),
        "tables": _jsonable(rows),
        "sql_rules": guard.describe_policy(),
    }


@mcp.tool()
def describe_table(table: str) -> dict:
    """Describe one table's columns, types and units.

    `table` may be qualified (`awk.hourlyforecasts`) or bare (`observations`,
    which resolves to the public schema).

    WORTH CALLING BEFORE WRITING SQL AGAINST observations. The column names are
    abbreviated and unlabelled — `tmp`, `dew`, `slp`, `wnd`, `vis`, `prp`, `cig`
    — and the units are not what you would guess. `tmp` is already Fahrenheit,
    not Celsius, and `slp` is inches of mercury, not hectopascals. The per-column
    descriptions returned here are the authoritative statement of that; they live
    as COMMENTs in the database (comment_tables.sql) rather than in a prompt, so
    they cannot drift out of date.
    """
    schema, _, name = table.strip().rpartition(".")
    schema = (schema or "public").lower().strip('"')
    name = name.lower().strip('"')

    if schema not in _SCHEMAS:
        raise ValueError(
            f"Unknown schema {schema!r}. Readable schemas are: "
            + ", ".join(_SCHEMAS)
            + ". Call list_schema to see the tables."
        )

    meta = db.query(
        db.WEATHERDATA, queries.DESCRIBE_TABLE_META, {"schema": schema, "table": name}
    )
    if not meta:
        raise ValueError(
            f"No readable table {schema}.{name}. Call list_schema for the "
            "ten tables this server exposes."
        )

    columns = db.query(
        db.WEATHERDATA, queries.DESCRIBE_TABLE, {"schema": schema, "table": name}
    )
    return {"table": _jsonable(meta)[0], "columns": _jsonable(columns)}


def _run_or_explain(sql: str) -> tuple[list[str], list[list]]:
    """Execute guarded SQL, turning database errors into model-readable ones.

    A query that passes the guard can still be wrong -- a misspelled column, a
    bad cast, an ambiguous reference. Postgres reports those precisely, and often
    with a HINT naming the correct identifier ('Perhaps you meant to reference
    the column "regions.state"'), which is exactly what a model needs to fix its
    own query on the next attempt.

    Left unhandled, psycopg raises a driver exception that MCP surfaces as a
    server fault, and the useful text is buried or lost. Re-raising as ValueError
    makes it a tool error the model reads and acts on, so the diagnostics Postgres
    already produced actually reach the thing that can use them.
    """
    try:
        return db.query_guarded(db.WEATHERDATA, sql)
    except psycopg.errors.QueryCanceled:
        # statement_timeout. Aggregating is the fix, so say so rather than just
        # reporting a timeout.
        raise ValueError(
            "Query exceeded the 30 second limit and was cancelled. Reduce the "
            "date range, or aggregate in SQL (GROUP BY, avg/count) instead of "
            "returning many rows. `observations` holds 9.17M rows, so an "
            "unfiltered scan with a join will not finish."
        ) from None
    except psycopg.errors.InsufficientPrivilege as exc:
        # The guard's allow-list and the grants disagree -- a real deployment
        # fault worth naming rather than passing off as a bad query.
        raise ValueError(
            f"The database refused access: {exc}. The query named a table this "
            "tool believes is readable, which means the grants and the "
            "allow-list have drifted. Re-run setup_role.sql."
        ) from None
    except psycopg.Error as exc:
        # Everything else: undefined column, bad cast, ambiguity, division by
        # zero. Pass Postgres's own message and hint straight through.
        detail = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
        hint = getattr(getattr(exc, "diag", None), "hint", None)
        raise ValueError(
            f"The database rejected the query: {detail}"
            + (f" HINT: {hint}" if hint else "")
            + " Call describe_table to check column names and types."
        ) from None


@mcp.tool()
def run_sql(sql: str, explain_only: bool = False) -> dict:
    """Run one read-only SELECT against the warehouse and return its rows.

    This is the general-purpose tool: use it for anything the typed tools do not
    cover. Call list_schema and describe_table first if you have not already —
    guessing at column names and units is the main way queries here go wrong.

    RULES, all enforced before the query reaches the database:
      * One statement. A leading WITH is fine; semicolon-separated statements
        are not.
      * SELECT only. No INSERT/UPDATE/DELETE/DDL — the database role holds
        SELECT and nothing else, so writes cannot succeed in any case.
      * Only the ten tables list_schema reports. System catalogs and
        information_schema are not readable; use describe_table instead.
      * Results are capped. A query with no LIMIT gets 500 rows; one asking for
        more than 5,000 is lowered to 5,000. `row_limit` in the response says
        which limit actually applied, and `truncated` says whether you hit it.

    A rejected query comes back as an error naming the rule it broke — read it
    and rewrite, rather than retrying the same statement.

    Set `explain_only` to validate and see the SQL that would run, plus the
    planner's cost estimate, without returning rows. Useful for checking an
    expensive-looking aggregate over the 9.17M-row observations table before
    committing to it.

    Queries are killed at 30 seconds. If you hit that, aggregate in SQL rather
    than returning rows and adding them up afterwards.
    """
    # guard.check both validates and rewrites (it forces the LIMIT), so the
    # statement executed is the one it returns — never the caller's original.
    try:
        checked_sql, report = guard.check(sql)
    except guard.SQLNotAllowed as exc:
        # Surfaced as ValueError so MCP reports it as a tool error the model can
        # read, rather than a server fault.
        raise ValueError(str(exc)) from None

    if explain_only:
        _, plan_rows = _run_or_explain(f"EXPLAIN {checked_sql}")
        return {
            "sql": checked_sql,
            "validated": True,
            "executed": False,
            "tables": report["tables"],
            "row_limit": report["row_limit"],
            "plan": [r[0] for r in plan_rows],
        }

    columns, rows = _run_or_explain(checked_sql)
    return {
        "sql": checked_sql,
        "executed": True,
        "tables": report["tables"],
        "columns": columns,
        "rows": _jsonable_rows(rows),
        "row_count": len(rows),
        "row_limit": report["row_limit"],
        # Truncation is only reported when the limit was IMPOSED, not when the
        # query set its own. `... ORDER BY x DESC LIMIT 5` returning 5 rows is a
        # complete top-5 answer, and flagging it as truncated would train a model
        # to hedge every ranking. A query that omitted LIMIT and came back with a
        # full 500 genuinely did lose rows, and must say so.
        "truncated": (
            len(rows) >= report["row_limit"]
            and report["limit_source"] != "caller"
        ),
        "limit_source": report["limit_source"],
    }


def main() -> None:
    """stdio transport — what Claude Desktop and `claude mcp add` launch."""
    mcp.run()


def main_http() -> None:
    """streamable-HTTP transport — what the Streamlit app connects to.

    Run as a long-lived service instead of a per-client subprocess:

        ./.venv/bin/python -m weather_mcp.server --http

    WHY NOT STDIO FOR THE WEB APP. Over stdio each client spawns its own copy of
    this process, which for a multi-session Streamlit app means one Python
    interpreter and one connection pool per browser tab. Over HTTP there is a
    single process with a single pool, which is what the 40-connection limit on
    this Postgres instance wants.

    BINDS TO 127.0.0.1 BY DEFAULT AND MUST STAY THAT WAY in deployment. There is
    no authentication on this endpoint -- anything that can reach it can read the
    warehouse. nginx terminates TLS and applies rate limits in front of it; a
    bind to 0.0.0.0 would publish the port on the EIP and bypass both. The
    weblog-dashboard deployment hit exactly this with Streamlit's own bind.

    `stateless_http=True` because Streamlit reruns the whole script on every
    interaction and reconnects rather than resuming a session; sessions would
    accumulate server-side with nothing reaping them.
    """
    host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_HTTP_PORT", "8770"))
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )



if __name__ == "__main__":
    # `--http` rather than a separate module so both transports share one
    # import path, and so the systemd unit and Claude Desktop launch the same
    # file with different arguments.
    if "--http" in sys.argv:
        main_http()
    else:
        main()

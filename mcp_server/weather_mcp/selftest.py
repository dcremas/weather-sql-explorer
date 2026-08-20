"""Exercise every tool against the live warehouse, without an MCP client.

    ./.venv/bin/python -m weather_mcp.selftest

Run it after setup, and again any time the schema or the tunnel changes. It
calls the same functions the MCP tools expose, so a pass here means the tools
work — the only thing it does not cover is the MCP transport itself.

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, Callable

from . import db, guard, server

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _preview(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("count", "returned", "observations", "stations"):
            if key in result and isinstance(result[key], int):
                return f"{result[key]} rows"
        return ", ".join(list(result)[:4])
    return str(result)[:60]


def check(name: str, fn: Callable[[], Any], expect_rows: bool = True) -> bool:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — the point is to report, not raise
        print(f"  {FAIL}  {name}")
        print(f"        {type(exc).__name__}: {str(exc).splitlines()[0]}")
        if "--verbose" in sys.argv:
            traceback.print_exc()
        return False

    if expect_rows and isinstance(result, dict):
        count = result.get("count", result.get("returned"))
        if count == 0:
            print(f"  {FAIL}  {name} — returned 0 rows (expected data)")
            return False

    print(f"  {PASS}  {name}  ({_preview(result)})")
    return True


def _missing(found: set[str]) -> dict:
    """Report which tables the allow-list expects but the database did not offer."""
    missing = sorted(guard.ALLOWED_TABLES - found)
    extra = sorted(found - guard.ALLOWED_TABLES)
    print(f"        missing: {missing or 'none'}; unexpected: {extra or 'none'}")
    return {"count": 0}


def _undocumented(tables: list[dict]) -> dict:
    """Report tables with no COMMENT — the model's only source of unit info."""
    bare = [f"{t['schema']}.{t['table_name']}" for t in tables if not t["description"]]
    print(f"        no description: {bare}  (run comment_tables.sql)")
    return {"count": 0}


def _limit_case(
    sql: str,
    expected_source: str,
    expected_truncated: bool,
    expected_limit: int | None = None,
) -> bool:
    """Check run_sql's row cap reporting on one query.

    The truncated flag is the part worth testing: it must be True only when the
    limit was IMPOSED. Reporting a deliberate `LIMIT 5` as truncated would teach
    a model to hedge every top-N answer it gives.
    """
    result = server.run_sql(sql)
    if result["limit_source"] != expected_source:
        print(f"        limit_source {result['limit_source']!r} != {expected_source!r}")
        return False
    if result["truncated"] != expected_truncated:
        print(f"        truncated {result['truncated']} != {expected_truncated}")
        return False
    if expected_limit is not None and result["row_limit"] != expected_limit:
        print(f"        row_limit {result['row_limit']} != {expected_limit}")
        return False
    return True


def expect_error(name: str, fn: Callable[[], Any], fragment: str) -> bool:
    """Assert a call is rejected, and rejected for the stated reason."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        if fragment.lower() in str(exc).lower():
            print(f"  {PASS}  {name}  (rejected as expected)")
            return True
        print(f"  {FAIL}  {name} — rejected, but not for the expected reason")
        print(f"        wanted {fragment!r}, got: {str(exc).splitlines()[0]}")
        return False
    print(f"  {FAIL}  {name} — call was accepted but should have been rejected")
    return False


def main() -> int:
    results: list[bool] = []
    station = None

    print("\nConnectivity and discovery")
    coverage = None
    try:
        coverage = server.data_coverage()
        obs = coverage["observations"]
        print(f"  {PASS}  data_coverage  ({obs['row_count']:,} rows, "
              f"{obs['station_count']} stations, latest {obs['latest']})")
        results.append(True)
    except Exception as exc:  # noqa: BLE001
        print(f"  {FAIL}  data_coverage")
        print(f"        {exc}")
        return 1  # nothing else can pass if this fails

    stations = server.list_stations(limit=5)
    results.append(check("list_stations (roster)", lambda: stations))
    if stations["count"]:
        station = stations["stations"][0]["station"]
        print(f"        using station {station} "
              f"({stations['stations'][0]['station_name']}) for subsequent checks")
    results.append(check("list_stations (state filter)",
                         lambda: server.list_stations(state="IL")))
    results.append(check("list_stations (name search)",
                         lambda: server.list_stations(name_contains="INTERNATIONAL")))

    if station is None:
        print("\nNo stations returned — cannot continue.")
        return 1

    latest = coverage["observations"]["latest"][:10]

    print("\nObservations")
    results.append(check("latest_observations (all)",
                         lambda: server.latest_observations()))
    results.append(check("get_observations",
                         lambda: server.get_observations(station, "2026-08-01", "2026-08-17")))
    results.append(check("summarize_observations (day)",
                         lambda: server.summarize_observations(
                             station, "2026-07-01", "2026-08-17", granularity="day")))
    results.append(check("summarize_observations (year, pressure)",
                         lambda: server.summarize_observations(
                             station, "2019-01-01", "2026-08-17",
                             measure="sea_level_pressure", granularity="year")))

    print("\nAnalysis")
    results.append(check("rank_stations (hottest, July 2026)",
                         lambda: server.rank_stations(
                             "2026-07-01", "2026-08-01", limit=5)))
    results.append(check("rank_stations (wettest, sum)",
                         lambda: server.rank_stations(
                             "2026-07-01", "2026-08-01", measure="precipitation",
                             aggregate="sum", limit=5)))
    results.append(check("compare_stations",
                         lambda: server.compare_stations(
                             [s["station"] for s in stations["stations"][:3]],
                             "2026-07-01", "2026-08-01")))
    results.append(check("temperature_anomaly (July 2026 vs 2019-2025)",
                         lambda: server.temperature_anomaly(2026, 7, limit=5)))

    print("\nForecasts")
    results.append(check("forecast_coverage",
                         lambda: server.forecast_coverage(limit=3)))
    results.append(check("get_forecast",
                         lambda: server.get_forecast(station, limit=5)))

    print("\nInput validation (these must be rejected)")
    results.append(expect_error("bad measure",
                                lambda: server.summarize_observations(
                                    station, "2026-01-01", "2026-02-01",
                                    measure="tmp; DROP TABLE observations"),
                                "unknown measure"))
    results.append(expect_error("bad granularity",
                                lambda: server.summarize_observations(
                                    station, "2026-01-01", "2026-02-01",
                                    granularity="century"),
                                "unknown granularity"))
    results.append(expect_error("bad date",
                                lambda: server.get_observations(
                                    station, "last tuesday", "2026-02-01"),
                                "ISO format"))
    results.append(expect_error("month out of range",
                                lambda: server.temperature_anomaly(2026, 13),
                                "month must be"))
    results.append(expect_error("too many stations",
                                lambda: server.compare_stations(
                                    [f"s{i}" for i in range(21)],
                                    "2026-01-01", "2026-02-01"),
                                "at most 20"))

    print("\nSchema discovery")
    schema = server.list_schema()
    results.append(check("list_schema", lambda: schema))
    # All ten tables must be reachable. This is the check that catches the
    # failure mode this suite exists for: a grant that was revoked out from
    # under the server. obs_baro_impact in particular is DROP/CREATEd by every
    # warehouse load, and only stays granted because
    # sql/analytics_slp_decrease.sql re-issues the GRANT -- if someone edits that
    # file, this is where it shows up.
    found = {f"{t['schema']}.{t['table_name']}" for t in schema["tables"]}
    results.append(check(
        f"all 10 tables readable ({len(found)} found)",
        lambda: {"count": 1} if found == guard.ALLOWED_TABLES else _missing(found),
    ))
    for table in sorted(guard.ALLOWED_TABLES):
        results.append(check(
            f"describe_table {table}",
            lambda t=table: {"count": len(server.describe_table(t)["columns"])},
        ))
    results.append(check(
        "every table carries a description",
        lambda: {"count": 1} if all(t["description"] for t in schema["tables"])
        else _undocumented(schema["tables"]),
    ))

    print("\nrun_sql — cross-database joins")
    # The whole point of the postgres_fdw setup: one statement spanning both
    # databases. If this fails, setup_fdw.sql has not been applied (or the
    # mcp_ro password was rotated without updating the user mapping, which
    # breaks foreign-table access while leaving local queries working).
    # NOTE the shape of this query. The obvious version --
    #   FROM observations o JOIN awk.hourlyforecasts f ON f.station = o.station
    #   GROUP BY o.station
    # times out, and it is worth understanding why because a model writes it
    # first every time: joining on `station` alone pairs each of the 9.17M
    # observation rows with all ~480 forecast rows for that station, so the
    # aggregate is computed over ~4.4 BILLION intermediate rows. The LIMIT does
    # not help, being applied after the GROUP BY.
    #
    # Aggregating each side to one row per station BEFORE joining gives the same
    # answer over 112 rows. That is what the awk.hourlyforecasts COMMENT tells
    # the model to do, and this check is here to keep the advice honest.
    results.append(check(
        "forecast joined to history in one query",
        lambda: {"count": server.run_sql(
            "with fc as (select station, avg(temp_f) fc_f "
            "            from awk.hourlyforecasts group by station), "
            "     hist as (select station, avg(tmp) hist_f "
            "              from public.observations where tmp is not null "
            "              group by station) "
            "select station, hist_f, fc_f from hist join fc using (station) "
            "limit 5")["row_count"]},
    ))
    results.append(check(
        "foreign table alone is readable",
        lambda: {"count": server.run_sql(
            "select count(*) from awk.hourlyforecasts")["row_count"]},
    ))
    results.append(check(
        "explain_only validates without executing",
        lambda: {"count": 1} if server.run_sql(
            "select station, avg(tmp) from observations group by station",
            explain_only=True)["executed"] is False else {"count": 0},
    ))

    print("\nrun_sql — row caps")
    results.append(check(
        "missing LIMIT is imposed and reported as truncated",
        lambda: {"count": 1} if _limit_case(
            "select station, tmp from observations where tmp > 100",
            expected_source="default", expected_truncated=True) else {"count": 0},
    ))
    results.append(check(
        "over-cap LIMIT is lowered to 5000",
        lambda: {"count": 1} if _limit_case(
            "select station from observations limit 99999",
            expected_source="capped", expected_truncated=True,
            expected_limit=guard.MAX_LIMIT) else {"count": 0},
    ))
    results.append(check(
        "caller's own LIMIT is not reported as truncated",
        lambda: {"count": 1} if _limit_case(
            "select state from regions limit 5",
            expected_source="caller", expected_truncated=False,
            expected_limit=5) else {"count": 0},
    ))

    print("\nrun_sql — the guard (these must all be rejected)")
    for label, sql, fragment in [
        ("DELETE",                "delete from regions", "only select"),
        ("UPDATE",                "update regions set state = 'X'", "only select"),
        ("INSERT",                "insert into regions values (1,'a','b','c')", "only select"),
        ("DROP",                  "drop table observations", "only select"),
        ("CREATE",                "create table t as select * from regions", "only select"),
        ("GRANT",                 "grant select on regions to public", "only select"),
        ("SET",                   "set default_transaction_read_only = off", "only select"),
        ("stacked statements",    "select 1 from regions; drop table regions", "only one is allowed"),
        ("DELETE inside a CTE",   "with d as (delete from regions returning *) select * from d", "not allowed anywhere"),
        ("pg_stat_activity",      "select * from pg_stat_activity", "not readable"),
        ("information_schema",    "select * from information_schema.tables", "not readable"),
        ("pg_catalog",            "select * from pg_catalog.pg_authid", "not readable"),
        ("a table not granted",   "select * from recipes", "not readable"),
        ("cross-database name",   "select * from recipes.public.observations", "not readable"),
        ("pg_read_file",          "select pg_read_file('/etc/passwd') from regions", "not available"),
        ("pg_sleep",              "select pg_sleep(60) from regions", "not available"),
        ("dblink",                "select * from dblink('host=x','select 1') as t(a int)", "not available"),
        ("FOR UPDATE",            "select * from observations for update", "locking clauses"),
        ("unparseable",           "this is not sql at all !!", "could not parse"),
        ("empty",                 "", "empty query"),
    ]:
        results.append(expect_error(label, lambda q=sql: server.run_sql(q), fragment))

    print("\nWrite protection")
    results.append(expect_error(
        "INSERT is refused by the database",
        lambda: db.query(db.WEATHERDATA,
                         "INSERT INTO observations (station) VALUES ('selftest')"),
        "read-only",
    ))
    # Belt and braces on the guard: prove the role itself would refuse a write
    # even if the guard were bypassed entirely, by going around it via
    # query_guarded. This is the claim the security model rests on, so it is
    # tested rather than asserted.
    results.append(expect_error(
        "DELETE refused even bypassing the guard",
        lambda: db.query_guarded(db.WEATHERDATA, "DELETE FROM regions"),
        "read-only",
    ))
    # And that the foreign tables are not a way around it -- awk_fdw is declared
    # updatable 'false', so this fails at the wrapper as well as at the role.
    results.append(expect_error(
        "write through the FDW is refused",
        lambda: db.query_guarded(db.WEATHERDATA,
                                 "DELETE FROM awk.hourlyforecasts"),
        "read-only",
    ))

    passed = sum(results)
    total = len(results)
    print(f"\n{'-' * 52}")
    print(f"{passed}/{total} checks passed"
          f"{'' if passed == total else '  — see failures above'}")
    print(f"warehouse latest observation: {latest}")
    db.close_all()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

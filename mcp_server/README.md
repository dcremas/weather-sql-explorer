# Weather Warehouse MCP Server

**Read-only conversational access to the GHCNh warehouse and the WeatherKit
forecast tables.** Thirteen tools — ten typed, plus schema introspection and a
guarded SQL passthrough — over a `SELECT`-only database role. Built 2026-08-20;
every command below was run and verified that day.

All ten tables in both databases are reachable, and `postgres_fdw` exposes the
forecast side inside `weatherdata` as schema `awk`, so **one SELECT can join
forecast against history**:

| Table | Contents |
|---|---|
| `public.observations` | 9,170,699 hourly observations, 112 US stations, 2019-01-01 → 2026-08-16 |
| `public.loc_subset` | the curated 112-station roster, one row per station per YEAR (896 rows) |
| `public.locations` | 28,350-station source reference; only the 112 have data |
| `public.regions` | US state → census region / sub_region, 51 rows |
| `public.obs_baro_impact` | 658,650 rows of 3/6/24h pressure deltas, rebuilt on every load |
| `awk.hourlyforecasts` | 53,760 forecast rows, same 112 stations, rolling ~2-week window |
| `awk.hf_baro_impact` | 53,760 rows; the forward-looking twin of `obs_baro_impact` |
| `awk.locations` | duplicate of `public.locations` — prefer the local one |
| `awk.regions` | duplicate, plus `tz_abbreviation` |
| `awk.time_zones` | abbreviation → utc_offset, 17 rows |

The station ids are identical across both databases — all 112 overlap — so a
station id from `list_stations` works unchanged against the forecast tools.

**Units are not what the column names suggest.** `tmp` is already Fahrenheit and
`slp` is inches of mercury. Both sides use the same units, so forecast-vs-history
comparisons need no conversion. This is recorded as `COMMENT`s in the database
(`comment_tables.sql`) and surfaced by `describe_table`, rather than living in a
prompt that can drift.

---

## 1. Why this exists, and when it does not

Two consumers, and they want different things:

1. **A surface with no shell** — Claude Desktop, claude.ai, mobile. In Claude
   Code this server is largely redundant, because the assistant already has
   shell, `psql` and the tunnel, which beats any fixed tool set.
2. **The text-to-SQL web app** (added 2026-08-20), which is the reason `run_sql`
   and the introspection tools exist. The app does not carry the schema in its
   prompt; it calls `list_schema` and `describe_table` and builds the prompt from
   what the database says about itself.

If you only ever ask these questions from the terminal, you do not need this.

---

## 2. Prerequisites — the SSH tunnel

> **This section is about running the server on your Mac.** There is a second
> deployment mode this README otherwise does not cover: the server also runs
> **on the EC2 box** as `weather-mcp.service` (`127.0.0.1:8770`), which is how
> the SQL Explorer web app reaches the warehouse. There the connection is local
> and **no tunnel is involved** — provisioning, the unit, and its user separation
> are documented in `../sql_explorer/deploy/README-deploy.md`. Everything below
> applies to the laptop mode only.

Public 5432 is closed to the internet (`../../NGINX-RUNBOOK.md` §11 has the full
two-layer firewall; `../../AWS-INVENTORY-RUNBOOK.md` §2 notes the private IP), so
from the Mac the tunnel is the **only** route to Postgres. Nothing here works
without it:

```bash
ssh -f -N -T -L 15432:127.0.0.1:5432 \
    -i ~/Automation/Raise/lambda-playground-1.pem ec2-user@<EC2_PUBLIC_IP>
```

Check it:

```bash
nc -z 127.0.0.1 15432 && echo "tunnel up" || echo "tunnel DOWN"
```

The server opens connections lazily, so it starts fine with the tunnel down —
only the first query fails, and it fails with a message telling you to restart
the tunnel rather than a bare "connection refused".

---

## 3. Setup

```bash
cd ~/projects/ec2-nginx/weather-sql-explorer/mcp_server

# 3a. Dependencies
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 3b. The read-only role (idempotent — safe to re-run)
psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d weatherdata        -f setup_role.sql
psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d apple_weatherkit   -f setup_role_apple_weatherkit.sql

# 3b-ii. The FDW and the schema comments. BOTH NEED A SUPERUSER, so they run on
# the box, not through the tunnel: CREATE EXTENSION and CREATE SERVER are
# superuser-only, and COMMENT requires ownership (obs_baro_impact is owned by
# ghcnh_etl). dustincremascoli is neither.
scp setup_fdw.sql comment_tables.sql ec2-user@<EC2_PUBLIC_IP>:/tmp/
ssh ec2-user@<EC2_PUBLIC_IP>
  sudo -u postgres psql -d weatherdata -v mcp_password="'<mcp_ro password>'" \
       -f /tmp/setup_fdw.sql
  sudo -u postgres psql -d weatherdata -f /tmp/comment_tables.sql

# 3c. Give the role a password and record it
psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d weatherdata \
     -c "ALTER ROLE mcp_ro PASSWORD 'pick-something-long';"
cp .env.example .env && chmod 600 .env    # then fill in MCP_DB_PASSWORD

# 3d. Verify
./.venv/bin/python -m weather_mcp.selftest
```

`setup_role.sql` will warn that it could **not** grant `obs_baro_impact` when run
through the tunnel — that table is owned by `ghcnh_etl`. Unlike before, this now
matters: `run_sql` can reach that table, so run the grant as `postgres` on the box.

**The `obs_baro_impact` grant does not stay granted by itself.**
`sql/analytics_slp_decrease.sql` does `DROP TABLE` + `CREATE TABLE AS` on every
warehouse load, and privileges go with the dropped table. That file therefore
re-issues the grant, `mcp_ro` included. If someone removes that line, the next
load silently revokes access and `run_sql` starts failing on that one table.
The "all 10 tables readable" selftest check is what catches it.

### Verified selftest output (2026-08-20)

```
61/61 checks passed
warehouse latest observation: 2026-08-16
```

The suite covers every tool against live data, `describe_table` on all ten
tables, a real cross-database join, the row-cap behaviour, **twenty guard
rejections**, and three write-protection checks — one of which deliberately
bypasses the guard to prove the database refuses the write on its own. If it does
not print 61/61, stop and fix that before wiring up a client.

---

## 4. Connecting a client

### Claude Desktop

Copy the `weather-warehouse` block out of
`claude_desktop_config.example.json` into:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

Restart Claude Desktop — it reads that file only at launch. **Use the absolute
path to `.venv/bin/python`**; Desktop does not inherit your shell `PATH`, so a
bare `python` will not resolve and the server will appear to fail silently.

### Claude Code

```bash
claude mcp add weather-warehouse \
  --scope user \
  -- /Users/dustincremascoli/projects/ec2-nginx/weather-sql-explorer/mcp_server/.venv/bin/python \
     -m weather_mcp.server
```

Worth having for testing the tools, though see §1 — in the terminal the CLI
route is generally better.

---

## 5. The tools

**Schema — start here for open-ended questions**

| Tool | Purpose |
|---|---|
| `list_schema` | The ten readable tables, with row estimates, descriptions, and the SQL rules. |
| `describe_table` | Columns, types and **units** for one table. Call before writing SQL against `observations` — the column names are abbreviated and the units are not guessable. |
| `run_sql` | One read-only SELECT. `explain_only=true` to validate and cost it without returning rows. |

**Discovery — start here for a named place**

| Tool | Purpose |
|---|---|
| `list_stations` | Turn a place name into a station id. Filter by state, region, sub-region, or name substring. |
| `data_coverage` | Row count, station count, date range, how far behind now. `by_year=true` for the per-year breakdown. |

**Observations**

| Tool | Purpose |
|---|---|
| `get_observations` | Raw hourly rows for one station over a range, all measures at once. |
| `summarize_observations` | One measure aggregated to hour / day / month / year — min, avg, max. |
| `latest_observations` | Newest reading per station; doubles as a freshness check. |

**Analysis**

| Tool | Purpose |
|---|---|
| `rank_stations` | Hottest / windiest / wettest over a window. `aggregate="sum"` for rainfall totals. |
| `compare_stations` | Side-by-side count/mean/min/max/stddev for 2–20 stations. |
| `temperature_anomaly` | Departure from each station's **own** monthly normal, ranked. |

**Forecasts**

| Tool | Purpose |
|---|---|
| `get_forecast` | Hourly WeatherKit forecast for a station. |
| `forecast_coverage` | The window the stored forecast spans — checks pipeline freshness. |

### Units

The column names do not reveal these; the tools return them explicitly.

```
temperature  °F     dew_point  °F      sea_level_pressure  inHg
wind_speed   mph    visibility miles   precipitation       inches   ceiling_height ft
```

### A note on `temperature_anomaly`

It compares each station against **itself**, so a desert station and a coastal
one are on equal footing — it measures departure from local normal, not absolute
temperature.

**The target year is always excluded from its own baseline**, even when it falls
inside the baseline range. Including it would blend the year being measured into
the mean it is measured against, pulling every anomaly toward zero — subtly, and
worse the shorter the baseline.

Verified example (July 2026 against a 2019–2025 baseline):

```
+4.93F  NATRONA COUNTY INTERNATIONAL AP (WY)
+4.17F  HECTOR INTERNATIONAL AIRPORT (ND)
+3.90F  BILLINGS LOGAN INTERNATIONAL AIRPORT (MT)
```

---

## 6. Security model

**This section changed on 2026-08-20.** It used to read: *"There is deliberately
no `run_sql` tool. A SQL passthrough puts model output directly into the query
planner, and no amount of prompting makes that safe."* There is now a `run_sql`
tool. The claim about prompting still stands — what was wrong was the conclusion
drawn from it.

What makes a passthrough acceptable is not persuading the model to behave, it is
that **the database cannot do the thing you are afraid of**. A `DELETE` arriving
from a poisoned prompt is not refused because the parser was clever; it is
refused because the role has no `DELETE` privilege. The guard exists to turn
safe-but-confusing failures into clear ones, not to be the thing standing between
a hostile query and your data.

Read-only is enforced in **four independent layers**:

1. **The role.** `mcp_ro` holds `SELECT` on ten tables and nothing else — no
   superuser, no createdb, no createrole, `NOINHERIT`. It carries
   `default_transaction_read_only=on`, `statement_timeout=30s`, and
   `idle_in_transaction_session_timeout=60s` as role-level defaults, so they
   apply even if a client forgets to ask. **This is the layer that matters.**
2. **The connection.** Every transaction sets `read_only=True`.
3. **The foreign server.** `awk_fdw` is declared `updatable 'false'`, so the
   forecast tables cannot be written through even by a superuser.
4. **The parser** (`guard.py`). Model-generated SQL is parsed with `sqlglot` and
   rejected unless it is a *single* read-only statement over an *allow-listed*
   table, with a `LIMIT` forced onto it. Decisions are made on the AST, never on
   keyword matching — comment injection and case tricks defeat blocklists, so
   none is used. It also refuses `pg_read_file`, `dblink`, `pg_sleep` and friends,
   the system catalogs, `information_schema`, and locking clauses.

The typed tools are unchanged: their SQL is still fixed and fully parameterised.

**Why `information_schema` is blocked but `describe_table` works.** The catalogs
are read by *fixed* queries in `queries.py`, filtered through
`has_table_privilege`, so a client sees structure for the ten tables it may read
and nothing else. `run_sql` cannot reach the catalogs at all, which keeps
`pg_stat_activity` (other sessions' query text) and `pg_settings` (filesystem
paths) out of reach.

Confirm the role is still what it should be:

```bash
psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d weatherdata -tA -c "
select table_name, string_agg(privilege_type,',') from information_schema.table_privileges
where grantee='mcp_ro' group by 1 order by 1;"
```

Verified 2026-08-20 — all ten tables, `SELECT` only, and writes proven to fail
in three separate selftest checks including one that bypasses the guard.

**One credential now lives in two places.** `postgres_fdw` will not connect as a
non-superuser without a password, so `mcp_ro`'s password is stored in
`pg_user_mapping` as well as in `.env`. **Rotating it requires changing both** —
change only the role and foreign-table queries start failing with an
authentication error while local queries keep working, which is a confusing way
to discover it. `pg_user_mapping` is superuser-only, so the password is not
exposed to `mcp_ro` itself.

**Network exposure: none added, in either mode.**

- *On the Mac:* the server reaches Postgres through the existing tunnel. Nothing
  is opened on the EC2 side.
- *On EC2:* `weather-mcp.service` binds **`127.0.0.1:8770` only** — verified
  2026-08-25 with `ss -ltnp`. It is not proxied by nginx and must not be; the only
  client is the co-located `sql-explorer` unit. The two run as different service
  users so that `sqlxapp` cannot read `/etc/weather-mcp/mcp.env`, which is what
  keeps the database password out of reach of the public web tier.

Public 5432 stays closed to the internet in both cases.

**The credential** lives only in `.env` (mode 600, gitignored). It is not in the
Claude Desktop config, not in the repo, and not in any command line.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not reach Postgres … tunnel is almost certainly down" | Restart the tunnel (§2). The message includes the exact command. |
| "Database authentication failed for role mcp_ro" | `MCP_DB_PASSWORD` in `.env` does not match what `ALTER ROLE` set. |
| Tools missing in Claude Desktop | `command` is not the absolute venv python path, or Desktop was not restarted. |
| `ModuleNotFoundError: mcp.server.fastmcp` | You are on MCP SDK v2, where `FastMCP` was replaced by `MCPServer` (`mcp.server.mcpserver`). This server already uses v2. |
| A query returns fewer rows than expected | Call `data_coverage` — the GHCNh feed is republished upstream and has regressed before. Missing recent data is usually a source condition, not a bug. Also check `truncated` in the `run_sql` response: a query with no `LIMIT` is capped at 500 rows. |
| `run_sql` says "exceeded the 30 second limit" on a forecast-vs-history join | Almost certainly the fan-out trap. `JOIN awk.hourlyforecasts ON station` alone pairs each of 9.17M observation rows with ~480 forecast rows. Aggregate each side in a CTE first, *then* join the CTEs. The table's `COMMENT` spells this out, and `describe_table` returns it. |
| Foreign-table queries fail on authentication, local ones work | The `mcp_ro` password was rotated without updating the FDW user mapping. Re-run `setup_fdw.sql`. |
| `run_sql` reports a table is "not readable" that `list_schema` lists | `guard.ALLOWED_TABLES` and the grants have drifted. They are two separate lists on purpose (so a wrong table name is rejected before a connection is opened) and the selftest's "all 10 tables readable" check compares them. |
| Everything reports the tunnel is down, but the tunnel is up | Fixed 2026-08-20. `QueryCanceled` subclasses `OperationalError`, so every statement timeout used to be reported as a dead tunnel. If this reappears, check `_QUERY_FAULTS` in `db.py`. |

Reproduce any tool outside MCP for debugging:

```bash
./.venv/bin/python -c "
from weather_mcp import server
import json; print(json.dumps(server.temperature_anomaly(2026, 7, limit=5), indent=2))"
```

---

## 8. Layout

```
mcp_server/
  README.md                            this file
  requirements.txt
  setup_role.sql                       mcp_ro + weatherdata grants
  setup_role_apple_weatherkit.sql      apple_weatherkit grants (per-database)
  setup_fdw.sql                        postgres_fdw -> schema `awk`  (SUPERUSER)
  comment_tables.sql                   the units, as COMMENTs          (OWNER)
  claude_desktop_config.example.json
  .env.example                         copy to .env, chmod 600
  weather_mcp/
    server.py      the 13 tools; MCPServer wiring
    queries.py     every fixed SQL statement + the identifier allow-lists
    guard.py       the gate model-generated SQL passes before the planner
    db.py          pooled read-only connections; `query` (fixed SQL) vs
                   `query_guarded` (validated dynamic SQL) are separate on
                   purpose, so dynamic SQL has exactly one entry point
    selftest.py    61 live checks — run after any change
```

Elsewhere in the repo, load-bearing for this server:

```
sql/analytics_slp_decrease.sql   rebuilds obs_baro_impact AND re-grants mcp_ro.
                                 Drop that GRANT and the next warehouse load
                                 silently revokes access to that table.
```

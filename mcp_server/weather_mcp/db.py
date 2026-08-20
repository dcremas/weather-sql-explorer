"""Connection handling for the weather MCP server.

Two databases sit behind this server, both reached through the SSH tunnel on
127.0.0.1:15432 (see README §Prerequisites):

    weatherdata       GHCNh observations — the 9.1M-row historical warehouse
    apple_weatherkit  WeatherKit hourly forecasts — the forward-looking side

Connections are pooled per database and opened lazily, so starting the server
with the tunnel down is not an error; only the first query fails, and it fails
with a message that says what to do about it.

Read-only is enforced in three independent places, because any one of them can
be got wrong:

  1. The `mcp_ro` role has `default_transaction_read_only = on` set at role
     level (setup_role.sql) and holds SELECT and nothing else.
  2. Every connection sets `read_only=True` on its transactions here.
  3. No query in queries.py is built from caller-supplied text — see the
     module docstring there.

Layer 1 is the one that actually matters; 2 and 3 exist so a mistake has to
happen three times to reach the warehouse.

ONE EXCEPTION, added 2026-08-20: `query_guarded` runs model-generated SQL for the
`run_sql` tool. Layer 3 does not apply to it, which is exactly why guard.py
exists — it parses the statement and proves it is a single read-only SELECT over
an approved table before it gets here. Layers 1 and 2 are unchanged and still
carry it. A fourth layer sits under the foreign tables: the `awk_fdw` server is
declared `updatable 'false'` (setup_fdw.sql).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Databases this server is allowed to touch. Anything not in here is
# unreachable regardless of what a tool asks for.
WEATHERDATA = "weatherdata"
APPLE_WEATHERKIT = "apple_weatherkit"
_DATABASES = (WEATHERDATA, APPLE_WEATHERKIT)

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Statement timeout applied per connection. The role carries its own 30s default
# (setup_role.sql); this is here so the server stays bounded even if it is ever
# pointed at a role that does not.
_STATEMENT_TIMEOUT_MS = 30_000

_pools: dict[str, ConnectionPool] = {}


class ConfigError(RuntimeError):
    """Raised when the server is not configured well enough to run."""


def _load_env() -> None:
    """Read mcp_server/.env into os.environ without adding a dependency.

    Values already present in the environment win, so a shell export or a
    Claude Desktop `env` block overrides the file rather than fighting it.
    """
    if not _ENV_PATH.exists():
        return
    for raw in _ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _conninfo(database: str) -> str:
    _load_env()
    password = os.environ.get("MCP_DB_PASSWORD")
    if not password:
        raise ConfigError(
            "MCP_DB_PASSWORD is not set. Put it in mcp_server/.env "
            f"(see .env.example), or export it before starting the server. "
            f"It is the password for the {os.environ.get('MCP_DB_USER', 'mcp_ro')} "
            "role created by setup_role.sql."
        )
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("MCP_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_DB_PORT", "15432")),
        user=os.environ.get("MCP_DB_USER", "mcp_ro"),
        password=password,
        dbname=database,
        connect_timeout=10,
        application_name="weather-mcp",
        options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
    )


def _pool(database: str) -> ConnectionPool:
    if database not in _DATABASES:
        # Defensive: no caller should be able to reach this, since database is
        # always a module constant rather than anything a tool argument sets.
        raise ValueError(f"database not permitted: {database!r}")
    if database not in _pools:
        _pools[database] = ConnectionPool(
            _conninfo(database),
            min_size=0,          # lazy: tunnel may legitimately be down at boot
            max_size=4,
            open=True,
            timeout=15,
            kwargs={"row_factory": dict_row},
        )
    return _pools[database]


def query(database: str, sql: str, params: Iterable[Any] | None = None) -> list[dict]:
    """Run one read-only SELECT and return its rows as dicts.

    `sql` is always a module-level constant from queries.py. `params` carries
    every caller-supplied value, bound by the driver — never interpolated.
    """
    try:
        with _pool(database).connection() as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()
    except ConfigError:
        raise
    except _QUERY_FAULTS:
        # Not a connection problem — see _QUERY_FAULTS. Let it through.
        raise
    except psycopg.OperationalError as exc:
        raise RuntimeError(_operational_hint(exc)) from exc


def query_guarded(database: str, sql: str) -> tuple[list[str], list[list]]:
    """Run one already-validated, model-generated statement for `run_sql`.

    SEPARATE FROM `query` ON PURPOSE. `query`'s contract is "the SQL is a
    constant from queries.py"; this one's is "the SQL is untrusted text that
    guard.check() has already rewritten and approved". Same pool, same read-only
    transaction — but a reviewer grepping for where dynamic SQL enters the
    database finds exactly one function, and it is named for what it does.

    THE CALLER MUST HAVE CALLED guard.check() FIRST. Nothing here re-validates;
    passing raw model output straight to this function defeats the guard. The
    only caller is server.run_sql.

    Returns (column_names, rows) rather than dicts, because a text-to-SQL result
    is a table for display: duplicate column names (`SELECT a.station, b.station`)
    are legal in SQL and would silently collapse into one key in a dict.
    """
    try:
        with _pool(database).connection() as conn:
            conn.read_only = True
            # A plain (non-dict) cursor: see the docstring on duplicate columns.
            with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
                cur.execute(sql)
                if cur.description is None:
                    # A statement returning no result set. The guard only admits
                    # SELECTs, so reaching this means the guard was bypassed.
                    raise RuntimeError(
                        "Query returned no result set. Only SELECT is supported."
                    )
                columns = [d.name for d in cur.description]
                return columns, [list(r) for r in cur.fetchall()]
    except ConfigError:
        raise
    except _QUERY_FAULTS:
        raise
    except psycopg.OperationalError as exc:
        raise RuntimeError(_operational_hint(exc)) from exc


# Errors that are SUBCLASSES of psycopg.OperationalError but are nothing to do
# with the connection. This distinction is not academic: it was a live bug.
#
# QueryCanceled (SQLSTATE 57014, statement_timeout) inherits from
# OperationalError, so the broad `except psycopg.OperationalError` below caught
# every timed-out query and reported "the SSH tunnel is almost certainly down" —
# on a perfectly healthy tunnel. The advice was to restart the tunnel, when the
# actual fix is to narrow the query. It also meant server.py's QueryCanceled
# handler could never fire, because the exception had already been rewritten into
# a RuntimeError by the time it got there.
#
# Anything listed here is re-raised unchanged so the caller can interpret it.
_QUERY_FAULTS = (
    psycopg.errors.QueryCanceled,        # statement_timeout, or a cancel request
    psycopg.errors.IdleInTransactionSessionTimeout,
)


def _operational_hint(exc: Exception) -> str:
    """Turn a connection failure into something actionable.

    The overwhelmingly common cause is the SSH tunnel being down, and the raw
    psycopg message ("connection refused") does not point there.
    """
    text = str(exc).strip()
    lowered = text.lower()
    if "authentication" in lowered or "password" in lowered:
        return (
            f"Database authentication failed for role "
            f"{os.environ.get('MCP_DB_USER', 'mcp_ro')}. Check MCP_DB_PASSWORD "
            f"in mcp_server/.env matches the password set by setup_role.sql.\n\n{text}"
        )
    if "does not exist" in lowered:
        return f"Database or role missing — has setup_role.sql been applied?\n\n{text}"
    # Only claim the tunnel is down when the error actually looks like a failure
    # to connect. Asserting it for every OperationalError sent people to restart
    # a working tunnel; the message is confident, so it was believed.
    connect_failure = any(
        marker in lowered
        for marker in (
            "could not connect", "connection refused", "timeout expired",
            "no route to host", "network is unreachable", "connection reset",
            "server closed the connection unexpectedly", "terminating connection",
        )
    )
    if connect_failure:
        return (
            "Could not reach Postgres on "
            f"{os.environ.get('MCP_DB_HOST', '127.0.0.1')}:"
            f"{os.environ.get('MCP_DB_PORT', '15432')}. The SSH tunnel is almost "
            "certainly down — public 5432 is closed, so the tunnel is the only "
            "route. Restart it with:\n\n"
            "  ssh -f -N -T -L 15432:127.0.0.1:5432 "
            "-i ~/Automation/Raise/lambda-playground-1.pem ec2-user@<EC2_PUBLIC_IP>\n\n"
            f"{text}"
        )
    return (
        "Postgres returned an operational error. The connection itself looks "
        f"fine, so this is most likely the query or the server's state:\n\n{text}"
    )


def close_all() -> None:
    """Close every pool. Called on server shutdown."""
    while _pools:
        _, pool = _pools.popitem()
        try:
            pool.close()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass

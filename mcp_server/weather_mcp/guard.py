"""The gate `run_sql` puts model-generated SQL through before it reaches Postgres.

WHY THIS FILE EXISTS AT ALL
---------------------------
Until 2026-08-20 this server deliberately had no `run_sql` tool, and queries.py
still carries the reasoning: "a SQL passthrough would put the model's output
directly into the query planner, and no amount of prompting makes that safe."

That reasoning was right about prompting and wrong about the conclusion. What
makes a passthrough safe is not persuading the model to behave, it is that the
*database* cannot do the thing you are afraid of. The `mcp_ro` role holds SELECT
on ten tables and nothing else, runs every transaction read-only, and times out
at 30s. A DELETE arriving from a compromised prompt does not get refused because
this file was clever; it gets refused because the role cannot delete.

So this file is the second line, not the first. Its job is to turn failures that
*would* be safe-but-confusing (a Postgres permission error surfacing mid-answer,
a cartesian join burning the whole statement_timeout) into clear, immediate
rejections, and to close the narrow gaps the role alone leaves open:

  * Reading tables the role can technically see but the app has no business
    exposing -- `pg_authid` is unreadable, but `pg_stat_activity` shows other
    sessions' query text, and `pg_settings` leaks filesystem paths.
  * Statement stacking. `SELECT 1; DROP TABLE x` -- the DROP fails on privileges,
    but relying on that is relying on the last line of defence.
  * Unbounded result sets. `SELECT * FROM observations` is a legal read-only
    query for 9.1M rows that no read-only role would stop.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not try to detect malicious intent, sanitise strings, or blocklist
keywords. Keyword blocklists on SQL are a well-known dead end -- comment
injection, string splitting and unicode confusables all defeat them. This works
the other way round: parse the statement into a tree, and reject anything whose
*shape* is not a single read-only projection over an approved table. Nothing is
matched textually; the decision is made on the parsed AST.
"""

from __future__ import annotations

try:
    import sqlglot
    from sqlglot import exp
except ModuleNotFoundError as _exc:  # pragma: no cover - dependency is pinned
    raise ModuleNotFoundError(
        "sqlglot is required for run_sql. Install it with "
        "`./.venv/bin/pip install -r requirements.txt`. run_sql refuses to run "
        "without it rather than falling back to pattern matching."
    ) from _exc

DIALECT = "postgres"

# Every relation run_sql may read, as schema.table. This is the same set the
# mcp_ro grants cover, restated here so a query naming something else is refused
# before it opens a connection instead of after Postgres denies it.
#
# `awk.*` are postgres_fdw foreign tables pointing at the apple_weatherkit
# database (see setup_fdw.sql) -- which is what lets one statement join forecast
# against history, the whole reason the FDW exists.
ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "public.observations",
        "public.locations",
        "public.loc_subset",
        "public.regions",
        "public.obs_baro_impact",
        "awk.hourlyforecasts",
        "awk.hf_baro_impact",
        "awk.locations",
        "awk.regions",
        "awk.time_zones",
    }
)

# Unqualified names resolve against public, matching the server's search_path.
DEFAULT_SCHEMA = "public"

# The database run_sql connects to. A three-part name naming this database is
# redundant but legal; naming any other one is a cross-database reference that
# Postgres cannot execute. The forecast tables are reached through the `awk`
# foreign schema, not by naming apple_weatherkit here.
CURRENT_DATABASE = "weatherdata"

# Hard ceiling on rows leaving the database. Applied by rewriting the query's own
# LIMIT, so it also caps a query that asked for more.
MAX_LIMIT = 5_000
DEFAULT_LIMIT = 500

# Statement types allowed at the top level. WITH is included because a CTE is how
# any interesting analytical query is written; its body is still walked, so a
# `WITH x AS (DELETE ... RETURNING)` is rejected on the inner node.
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)

# Node types that mutate, change session state, or read outside the tables above.
# Checked by class rather than by keyword, so `/*c*/DROP` and `DrOp` are the same
# node and neither needs special handling.
_FORBIDDEN_NODES: tuple[type, ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Merge, exp.Command, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Set, exp.Use, exp.Copy,
)

# Functions that read the filesystem, reach the network, or run arbitrary code.
# None are reachable by mcp_ro's privileges, but a clear rejection beats a
# permission error surfacing halfway through an answer.
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
        "lo_import", "lo_export", "dblink", "dblink_exec", "pg_sleep",
        "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
        "pg_logical_emit_message", "query_to_xml", "pg_read_server_files",
        "set_config", "pg_advisory_lock", "pg_advisory_xact_lock",
    }
)


class SQLNotAllowed(ValueError):
    """A query was rejected. The message is written to be shown to the model.

    Rejections are phrased as instructions rather than complaints, because the
    consumer is a model that will retry: saying which tables exist gets a usable
    second attempt, where "permission denied" gets the same query again.
    """


def _qualified_names(tree: exp.Expression) -> set[str]:
    """Collect every real table the statement reads, as schema.table.

    CTE names are excluded -- `WITH recent AS (...) SELECT * FROM recent` reads
    `recent`, which is not a table and must not be checked against the allow-list
    or every CTE query would be rejected.
    """
    cte_names = {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }

    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        schema = (table.db or DEFAULT_SCHEMA).lower()

        # A three-part name (catalog.schema.table) is a cross-database reference,
        # which Postgres does not implement -- `recipes.public.observations` would
        # otherwise reduce to public.observations here, pass the allow-list, and
        # then fail in the planner with "cross-database references are not
        # implemented". Carry the catalog through so it fails the allow-list
        # instead and the model is told why.
        catalog = (table.catalog or "").lower()
        if catalog and catalog != CURRENT_DATABASE:
            names.add(f"{catalog}.{schema}.{name}")
            continue

        names.add(f"{schema}.{name}")
    return names


def check(sql: str) -> tuple[str, dict]:
    """Validate one statement and return (rewritten_sql, report).

    Raises SQLNotAllowed with a model-readable explanation if the statement is
    not a single bounded read over the approved tables. The returned SQL is the
    statement to actually execute -- it may differ from the input, because a
    missing or oversized LIMIT is rewritten.
    """
    if not sql or not sql.strip():
        raise SQLNotAllowed("Empty query. Provide a single SELECT statement.")

    # 1. Parse. A statement that does not parse never reaches the database, so a
    #    syntax error costs no connection and no timeout.
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:  # sqlglot raises several types
        raise SQLNotAllowed(
            f"Could not parse that as PostgreSQL: {exc}. "
            "Send one plain SELECT statement."
        ) from exc

    statements = [s for s in statements if s is not None]
    if not statements:
        raise SQLNotAllowed("No statement found. Provide a single SELECT.")

    # 2. Exactly one statement. This is what stops `SELECT 1; DROP TABLE x` --
    #    the DROP would fail on privileges anyway, but it should never be sent.
    if len(statements) > 1:
        raise SQLNotAllowed(
            f"{len(statements)} statements found; only one is allowed. "
            "Remove the semicolons and send a single SELECT."
        )

    tree = statements[0]

    # 3. Read-only shape, decided on the node type rather than on keywords.
    if not isinstance(tree, _ALLOWED_ROOTS):
        raise SQLNotAllowed(
            f"Only SELECT queries are allowed; this is "
            f"{type(tree).__name__.upper()}. The database role is read-only, so "
            "writes cannot succeed regardless."
        )

    for node_type in _FORBIDDEN_NODES:
        found = tree.find(node_type)
        if found is not None:
            raise SQLNotAllowed(
                f"{node_type.__name__.upper()} is not allowed anywhere in the "
                "query, including inside a CTE or subquery. Read-only SELECT only."
            )

    # 4. No row locks. `SELECT ... FOR UPDATE` takes a write lock, so the
    #    read-only transaction rejects it -- but with "cannot execute SELECT FOR
    #    UPDATE in a read-only transaction", which reads like the query was
    #    nearly fine. Rejecting it here says what to do instead. sqlglot keeps
    #    these in args["locks"] rather than as a findable child node.
    if tree.args.get("locks"):
        raise SQLNotAllowed(
            "Locking clauses (FOR UPDATE / FOR SHARE) are not allowed -- they "
            "take write locks and this role is read-only. Drop the FOR UPDATE; "
            "a plain SELECT returns the same rows."
        )

    # 5. No functions that escape the database.
    for func in tree.find_all(exp.Anonymous):
        name = (func.this or "")
        if isinstance(name, str) and name.lower() in _FORBIDDEN_FUNCTIONS:
            raise SQLNotAllowed(
                f"The function {name}() is not available to this tool."
            )

    # 6. Only approved tables. Catches both a wrong guess at a table name and an
    #    attempt to read the system catalogs.
    referenced = _qualified_names(tree)
    if not referenced:
        raise SQLNotAllowed(
            "The query does not read any table. Query one of: "
            + ", ".join(sorted(ALLOWED_TABLES))
        )

    disallowed = referenced - ALLOWED_TABLES
    if disallowed:
        raise SQLNotAllowed(
            "Not readable by this tool: "
            + ", ".join(sorted(disallowed))
            + ". Available tables are: "
            + ", ".join(sorted(ALLOWED_TABLES))
            + ". System catalogs and information_schema are not exposed -- call "
            "list_schema or describe_table for structure instead."
        )

    # 7. Bound the result set. A read-only role happily returns 9.1M rows, so the
    #    cap is enforced here by rewriting the tree rather than by trusting the
    #    model to include a LIMIT.
    applied_limit, limit_source, tree = _apply_limit(tree)

    # pretty=True: the regenerated statement is what gets DISPLAYED as well as
    # executed, and sqlglot's default output is a single unbroken line. A 300-
    # character one-liner overflows any code block it is shown in, which matters
    # here because the whole point of the app in front of this is that a visitor
    # can read the query. Postgres is indifferent to the whitespace.
    return tree.sql(dialect=DIALECT, pretty=True), {
        "tables": sorted(referenced),
        "row_limit": applied_limit,
        # Which limit is in force matters for reporting truncation honestly. A
        # query that asked for LIMIT 5 and got 5 rows is a finished top-5 answer;
        # one that asked for nothing and got 500 had rows withheld. Both look
        # identical from the row count alone, so the caller is told the source.
        "limit_source": limit_source,
    }


def _apply_limit(tree: exp.Expression) -> tuple[int, str, exp.Expression]:
    """Force a LIMIT onto the statement.

    Returns (limit_now_in_force, where_it_came_from, tree). The source is one of:

      "caller"   the query's own LIMIT, within the cap, left untouched
      "default"  the query had no LIMIT and DEFAULT_LIMIT was added
      "capped"   the query asked for more than MAX_LIMIT and was lowered

    The distinction is not cosmetic. "caller" means a full page of rows is the
    complete answer the query asked for; "default" and "capped" mean rows were
    withheld and whoever reads the result needs to know that.

    Set operations (UNION and friends) are wrapped in a subquery rather than
    having a LIMIT attached, because `a UNION b LIMIT 5` binds the limit to `b`
    in some dialects and to the union in others -- wrapping makes it unambiguous.
    """
    existing = tree.args.get("limit") if isinstance(tree, exp.Select) else None

    if existing is not None:
        value = existing.expression
        current: int | None = None
        if isinstance(value, exp.Literal) and value.is_int:
            current = int(value.name)
        if current is not None and current <= MAX_LIMIT:
            return current, "caller", tree
        # Non-literal (a parameter or expression) or over the cap: replace it.
        tree.set("limit", exp.Limit(expression=exp.Literal.number(MAX_LIMIT)))
        return MAX_LIMIT, "capped", tree

    if isinstance(tree, exp.Select):
        tree.set("limit", exp.Limit(expression=exp.Literal.number(DEFAULT_LIMIT)))
        return DEFAULT_LIMIT, "default", tree

    wrapped = exp.Select().select(exp.Star()).from_(exp.Subquery(this=tree, alias="q"))
    wrapped.set("limit", exp.Limit(expression=exp.Literal.number(DEFAULT_LIMIT)))
    return DEFAULT_LIMIT, "default", wrapped


def describe_policy() -> str:
    """The rules, phrased for a system prompt.

    Kept next to the code that enforces it so the two cannot drift -- a prompt
    that promises something the guard rejects wastes a model turn on every query.
    """
    return (
        "SQL rules: one read-only SELECT (a leading WITH is fine). No INSERT, "
        "UPDATE, DELETE or DDL -- the database role cannot write. No semicolons "
        f"or multiple statements. Results are capped at {MAX_LIMIT} rows and "
        f"default to {DEFAULT_LIMIT} if you omit LIMIT. Readable tables:\n"
        + "\n".join(f"  {t}" for t in sorted(ALLOWED_TABLES))
        + "\nSystem catalogs and information_schema are not readable; use "
        "list_schema or describe_table instead."
    )

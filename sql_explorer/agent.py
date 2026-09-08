"""The text-to-SQL agent: Gemini, reached through MCP tools, driven from Streamlit.

SHAPE OF THE THING
------------------
    Streamlit (sync)
      -> one background asyncio loop, for the app's whole life
        -> LangChain agent (Gemini 3.8 Flash)
          -> MCP tools over streamable HTTP to 127.0.0.1:8770
            -> mcp_ro on Postgres, SELECT-only

The app holds NO database credentials and builds NO SQL. It cannot: the only way
it reaches data is by calling MCP tools, and the only tool that runs SQL runs it
through guard.py as a read-only role. That is the reason for the extra hop -- the
web tier being compromised does not put the warehouse at risk, because the web
tier was never trusted with it.

TWO VERSION TRAPS, BOTH OF WHICH COST TIME
------------------------------------------
1. `langchain-mcp-adapters` 0.3.1 does not work with the `mcp` 2.x SDK -- it
   imports `RequestContext` from `mcp.shared.context`, which v2 moved:

       ImportError: cannot import name 'RequestContext' from 'mcp.shared.context'

   THIS APP THEREFORE PINS `mcp<2`, while the SERVER runs `mcp>=2` in its own
   venv. That is not an oversight and the two must not be unified. They are
   separate processes that share a wire protocol, not a library, and a v1.29
   client talking to a v2.0 server is verified working (13 tools discovered,
   `run_sql` executed, 2026-08-20). Do not "fix" this by upgrading `mcp` here;
   it breaks the import above.

2. The MCP server must be reachable BEFORE the agent is built, because tool
   discovery is a live call. A dead server surfaces as an empty tool list and the
   model then hallucinates answers with no data, which looks like a model problem
   and is not. `build_agent` checks explicitly and fails loudly instead.

WHY A BACKGROUND EVENT LOOP
---------------------------
Streamlit re-executes the script top to bottom on every interaction, from a
thread it owns. The MCP client and the agent are async, and its sessions are tied
to the loop that created them, so `asyncio.run(...)` per interaction would build
a fresh loop each time, strand the previous connections, and eventually raise
"attached to a different loop". One long-lived loop on a daemon thread, cached
for the process, keeps the client valid across reruns.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient

import budget


def _load_env() -> None:
    """Read .env next to this file, without adding a dependency.

    Values already in the environment win, so systemd's EnvironmentFile and a
    shell export both override the file rather than fighting it.
    """
    path = Path(__file__).parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Read at import, BEFORE the constants below. Both of them read the environment,
# so a .env loaded any later than this is a .env that silently does nothing.
_load_env()

MCP_URL = os.environ.get("SQLX_MCP_URL", "http://127.0.0.1:8770/mcp")
MODEL = os.environ.get("SQLX_MODEL", "gemini-3.8-flash")

# Only the three SQL-shaped tools are exposed, out of the server's thirteen.
#
# The typed tools (list_stations, rank_stations, temperature_anomaly, ...) would
# answer many of these questions more reliably, and that is exactly why they are
# left out: this app exists to show a question becoming SQL. If the agent could
# satisfy "which station is hottest" with a single typed call, the demo would
# display a tool name instead of a query, which is the thing worth seeing.
#
# It also keeps the prompt small. Thirteen tool schemas on every turn is a real
# per-question cost on a public endpoint.
EXPOSED_TOOLS = ("list_schema", "describe_table", "run_sql")

SYSTEM_PROMPT = """\
You are a data analyst answering questions about a weather warehouse by writing \
PostgreSQL.

HOW TO WORK
1. Call list_schema once to see the readable tables and the SQL rules.
2. Call describe_table for every table you intend to query, BEFORE writing SQL. \
The column names are abbreviated (tmp, dew, slp, wnd, vis, prp, cig) and the \
units are not guessable -- describe_table returns them and they are \
authoritative.
3. Write one SELECT and run it with run_sql.

WHAT MATTERS IN THE ANSWER
- Always state units. `tmp` is already Fahrenheit; never convert it from Celsius.
- Station ids are opaque 11-character codes. Join to public.loc_subset for \
human-readable station_name and state, and show names, not ids. loc_subset has \
one row per station per YEAR, so use SELECT DISTINCT when you only want names.
- Forecast data (awk.*) and history (public.*) are in the same database and can \
be joined in one query on `station`. Aggregate each side in a CTE first, then \
join the CTEs -- joining the raw tables on station alone produces billions of \
intermediate rows and times out.
- When you bucket by time, alias the bucket clearly -- `AS year`, `AS month`, \
`AS day` -- not `AS y`. The chart picks the axis from the column name, so a vague \
alias costs the user a chart.
- If run_sql returns `truncated: true`, say so; the result is a partial answer.
- If a query is rejected or errors, read the message and fix the query. Do not \
retry the same statement.

STYLE
Answer in a few sentences. Lead with the finding, not with a description of what \
you did. Give numbers to one decimal place with their units. Do not paste the SQL \
into your answer -- it is already displayed to the user separately. If the data \
cannot answer the question, say so plainly rather than substituting something \
adjacent.
"""


# --------------------------------------------------------------------------- #
# The background event loop
# --------------------------------------------------------------------------- #

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide event loop, starting its thread on first use."""
    global _loop
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        loop = asyncio.new_event_loop()
        # daemon=True so the thread cannot keep the process alive on shutdown --
        # Streamlit does not join threads it did not start.
        threading.Thread(
            target=loop.run_forever, name="sqlx-agent-loop", daemon=True
        ).start()
        _loop = loop
        return loop


def run_sync(coro, timeout: float = 180.0) -> Any:
    """Run a coroutine on the background loop and block until it finishes.

    The timeout is the backstop for the whole agent turn. Each individual SQL
    statement is already capped at 30s server-side, but an agent can issue
    several in sequence, so the turn needs its own ceiling or a pathological
    question could hold a Streamlit worker indefinitely.
    """
    future: Future = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    return future.result(timeout=timeout)


# --------------------------------------------------------------------------- #
# Building the agent
# --------------------------------------------------------------------------- #


class SetupError(RuntimeError):
    """Configuration or connectivity problem, phrased for the operator."""


# Discovered once and reused. Tool discovery is a live HTTP call and the client's
# sessions belong to the loop that created them, so this is deliberately not
# rebuilt per caller -- see WHY A BACKGROUND EVENT LOOP above.
_tools_by_name: dict[str, Any] | None = None
_client: Any = None


async def _discover_tools() -> dict[str, Any]:
    """The three exposed MCP tools, by name. Reaches the server on first call.

    Split out of `_build` because the data-model page needs `list_schema` and
    `describe_table` WITHOUT an LLM. Building the model to read a table's columns
    would make the schema reference depend on GOOGLE_API_KEY being set and on the
    provider being up, neither of which has anything to do with reading
    pg_catalog. Nothing here spends a token.
    """
    global _tools_by_name, _client
    if _tools_by_name is not None:
        return _tools_by_name

    client = MultiServerMCPClient(
        {"weather": {"transport": "streamable_http", "url": MCP_URL}}
    )

    try:
        discovered = await client.get_tools()
    except Exception as exc:  # noqa: BLE001 - becomes an on-page message
        raise SetupError(
            f"Could not reach the MCP server at {MCP_URL} ({exc}). "
            "Start it with:\n\n"
            "  cd ../mcp_server && ./.venv/bin/python -m weather_mcp.server --http\n\n"
            "On the box it is the `weather-mcp` systemd service."
        ) from exc

    tools = {t.name: t for t in discovered if t.name in EXPOSED_TOOLS}
    missing = set(EXPOSED_TOOLS) - set(tools)
    if missing:
        # An older server build, or a partial start. Better to say so than to run
        # with a crippled tool set and let the model improvise.
        raise SetupError(
            f"The MCP server is running but does not expose {sorted(missing)}. "
            f"It offered: {sorted(t.name for t in discovered)}. Update the server."
        )

    _client = client
    _tools_by_name = tools
    return tools


def call_tool(name: str, arguments: dict | None = None, timeout: float = 45.0) -> Any:
    """Call one MCP tool directly, with no model involved.

    This is how the data-model page reads the schema: `list_schema` and
    `describe_table` return the table and column COMMENTs straight out of
    pg_catalog, so the documentation on the page is the database's own and cannot
    drift away from it. Because no LLM is in the path, it is also free and is not
    charged to the budget ledger.
    """

    async def _run() -> Any:
        tools = await _discover_tools()
        tool = tools.get(name)
        if tool is None:
            raise SetupError(f"The MCP server does not expose {name!r}.")
        return await tool.ainvoke(arguments or {})

    return run_sync(_run(), timeout=timeout)


async def _build() -> tuple[Any, list[str]]:
    if not os.environ.get("GOOGLE_API_KEY"):
        raise SetupError(
            "GOOGLE_API_KEY is not set. Put it in sql_explorer/.env "
            "(see .env.example) or export it before starting Streamlit."
        )

    tools = await _discover_tools()

    llm = ChatGoogleGenerativeAI(
        model=MODEL,
        temperature=0,  # SQL generation: same question, same query.
        max_retries=2,
        # 3.8 Flash reasons by default and cannot be told not to -- the API
        # accepts low/medium/high and rejects "minimal". Left at its default it
        # spends roughly 78k tokens on the Denver example question against 3.7
        # Flash's 45k, which at SQLX_MAX_DAILY_TOKENS=1.5M is the difference
        # between ~33 and ~19 questions a day, i.e. two visitors exhausting the
        # budget. Measured 2026-09-08, five runs per setting against the live
        # MCP server: low takes it to ~58k without changing the answers. That
        # is most of the gap closed but not all of it -- 3.8 on this workload
        # still costs more than 3.7 did, ~25 questions a day against ~33, and
        # raising the step limit to 28 is part of why: runs that used to be cut
        # off at 20 now finish, and a finished run costs more than a truncated
        # one. It is not a fix for the step limit on its own -- see
        # MAX_AGENT_STEPS in budget.py.
        reasoning_effort="low",
    )
    agent = create_agent(llm, list(tools.values()), system_prompt=SYSTEM_PROMPT)
    return agent, list(tools)


def build_agent() -> tuple[Any, list[str]]:
    """Build the agent, blocking. Cache the result -- this does network I/O."""
    return run_sync(_build(), timeout=60.0)


# --------------------------------------------------------------------------- #
# Asking a question
# --------------------------------------------------------------------------- #


def _tokens_from(messages: list) -> int:
    """Total token usage across the turn, for the budget ledger.

    Read from usage_metadata on each AI message. Providers have moved this field
    more than once, so a missing value is treated as unknown rather than zero --
    an unknown that reads as zero is a question that costs nothing according to
    the ledger, which is how a budget silently stops working. When nothing is
    reported at all the caller charges a flat estimate instead.
    """
    total = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            total += int(usage.get("total_tokens") or 0)
    return total


# Charged when the provider reports no usage at all, so an unmeasurable question
# still costs something. Deliberately pessimistic.
UNKNOWN_QUESTION_TOKENS = 20_000


def ask(agent: Any, question: str, session_questions: int) -> dict:
    """Answer one question. Raises budget.BudgetExceeded before spending anything.

    Returns the answer text, every SQL statement the agent ran with its result,
    and the token count charged.
    """
    budget.check_can_spend(session_questions)

    charged = UNKNOWN_QUESTION_TOKENS
    try:
        result = run_sync(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": question}]},
                # Hard stop on the tool-call loop. An agent retrying a failing
                # query is the expensive failure mode and a question count
                # cannot see it.
                {"recursion_limit": budget.MAX_AGENT_STEPS},
            )
        )
    except Exception:
        # Charge the estimate even on failure: a crashed or looping run still
        # spent tokens, and exempting failures is a hole in the daily cap.
        budget.record(charged)
        raise

    messages = result.get("messages", [])
    measured = _tokens_from(messages)
    charged = measured or UNKNOWN_QUESTION_TOKENS
    ledger = budget.record(charged)

    return {
        "answer": _final_text(messages),
        "queries": extract_queries(messages),
        "tokens": charged,
        "tokens_measured": bool(measured),
        "ledger": ledger,
        "steps": len(messages),
    }


def _final_text(messages: list) -> str:
    """The model's closing answer.

    Gemini returns content as a list of parts rather than a plain string, so the
    text has to be reassembled; taking `str(content)` renders a Python repr of
    dicts on the page.
    """
    for message in reversed(messages):
        if type(message).__name__ != "AIMessage":
            continue
        if getattr(message, "tool_calls", None):
            continue
        content = message.content
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            joined = "\n".join(p for p in parts if p).strip()
            if joined:
                return joined
    return "The model returned no answer. Try rephrasing the question."


def extract_queries(messages: list) -> list[dict]:
    """Pair each run_sql call with its result, in the order they were executed.

    This is what the page shows: the point of the app is the SQL, so the trace
    is a feature rather than debug output. Pairing is by tool_call_id because an
    agent can have several calls in flight and matching by order alone gets them
    crossed.
    """
    calls: dict[str, dict] = {}
    order: list[str] = []

    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") != "run_sql":
                continue
            call_id = call.get("id") or f"call{len(order)}"
            calls[call_id] = {
                "requested_sql": (call.get("args") or {}).get("sql", ""),
                "explain_only": bool((call.get("args") or {}).get("explain_only")),
            }
            order.append(call_id)

    for message in messages:
        if type(message).__name__ != "ToolMessage":
            continue
        call_id = getattr(message, "tool_call_id", None)
        if call_id not in calls:
            continue
        calls[call_id].update(_parse_tool_result(message.content))

    return [calls[cid] for cid in order if cid in calls]


def _parse_tool_result(content: Any) -> dict:
    """Decode an MCP tool result into the fields the page renders.

    MCP returns content as a list of typed blocks; the payload is JSON inside a
    text block. A rejected query arrives as an error string rather than JSON,
    which is not an exception here -- it is the guard doing its job, and the
    message is worth showing.
    """
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
            elif isinstance(block, str):
                text += block

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return {"error": text.strip() or "The tool returned nothing.", "rows": []}

    if not isinstance(payload, dict):
        return {"error": str(payload), "rows": []}

    return {
        "sql": payload.get("sql", ""),
        "columns": payload.get("columns", []),
        "rows": payload.get("rows", []),
        "row_count": payload.get("row_count", 0),
        "row_limit": payload.get("row_limit"),
        "truncated": bool(payload.get("truncated")),
        "limit_source": payload.get("limit_source"),
        "plan": payload.get("plan"),
        "tables": payload.get("tables", []),
    }

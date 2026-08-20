"""Weather Warehouse SQL Explorer — ask in English, see the SQL, see the data.

    ./run.sh                      # local, needs the SSH tunnel + the MCP server
    streamlit run app.py          # if you have already sorted the environment

FOUR CONSTRAINTS THAT BREAK THIS APP IF REMOVED
-----------------------------------------------
1. `st.plotly_chart(..., theme=None)` on EVERY chart. The default
   `theme="streamlit"` silently repaints figures in Streamlit's own palette and
   discards every colour decision in theme.py -- including the CVD validation
   those colours were chosen to pass. The chart still looks fine, which is why
   this keeps getting reintroduced.

2. The agent is built inside `@st.cache_resource`. Streamlit re-runs this whole
   script on every interaction; without the cache each keystroke-completed
   question would rebuild the MCP client, re-discover tools over HTTP, and
   abandon the previous connection.

3. `budget.check_can_spend` runs BEFORE the model is called, and `budget.record`
   runs even when the call fails. This endpoint is public and every question
   spends real tokens; a failed or looping agent run still costs money, so
   exempting failures from the ledger is a hole in the daily cap.

4. The result table is always rendered, never behind a collapsed expander. It is
   the accessibility relief for the light palette's sub-3:1 contrast slots (see
   theme.py) as well as the honest view of what the query returned.

WHAT IS ON THE PAGE AND WHY
---------------------------
The generated SQL is displayed as a first-class part of the answer, not hidden in
a debug panel. The point of the app is that the question became a query; a
visitor who cannot see the query has to take the number on faith, and a wrong
answer is indistinguishable from a right one.
"""

from __future__ import annotations

import streamlit as st

import agent as agent_module
import budget
import charts

st.set_page_config(
    page_title="Weather Warehouse SQL Explorer",
    page_icon="🌤️",
    layout="wide",
)

EXAMPLES = [
    "Which 5 stations are forecast furthest above their own August average temperature?",
    "What were the 10 hottest hours ever recorded, and where?",
    "How has the average August temperature changed each year since 2019?",
    "Which states have the windiest weather on average?",
    "Show the daily average temperature at Denver for the last 30 days of data.",
    "Which stations show the sharpest 24-hour pressure drops?",
]


@st.cache_resource(show_spinner="Connecting to the MCP server…")
def get_agent():
    """Build the agent once per process. See constraint 2 in the module docstring."""
    return agent_module.build_agent()


def render_query(index: int, query: dict, key: str) -> None:
    """Show one executed statement: the SQL, then a chart, then the table.

    `key` must be unique across the whole page AND stable across reruns.
    Streamlit derives an element id from the element type plus its arguments, so
    two charts built from similar frames collide and the page dies with
    StreamlitDuplicateElementId -- which is what happened the first time a second
    answer was added to the history. It is not a hypothetical: two questions about
    the same table produce near-identical figures.

    Stability matters as much as uniqueness. The key is built from the exchange's
    own id, not its position in the list: history is prepended, so every previous
    answer's position shifts when a new one arrives, and position-based keys would
    re-key every chart on the page on every question.
    """
    label = f"Query {index}" if index else "Query"

    if query.get("error"):
        st.markdown(f"**{label}** — rejected")
        if query.get("requested_sql"):
            st.code(query["requested_sql"], language="sql")
        # A rejection is the guard working, not a crash. Shown as a warning so it
        # reads as "the query was not allowed", not "the site is broken".
        st.warning(query["error"])
        return

    st.markdown(f"**{label}**")
    st.code(query.get("sql") or query.get("requested_sql", ""), language="sql")

    if query.get("plan"):
        with st.expander("Query plan (validated, not executed)"):
            st.code("\n".join(query["plan"]), language="text")
        return

    columns = query.get("columns") or []
    rows = query.get("rows") or []
    if not columns:
        st.caption("No result set returned.")
        return

    frame = charts.build_frame(columns, rows)
    chart_plan = charts.plan(frame)

    if chart_plan["chartable"]:
        figure = charts.figure(frame, chart_plan)
        # theme=None is load-bearing — see constraint 1.
        st.plotly_chart(figure, use_container_width=True, theme=None, key=f"{key}-chart")
        if chart_plan["dropped"]:
            st.caption(
                "Not charted: "
                + ", ".join(f"`{c}`" for c in chart_plan["dropped"])
                + " — too different in magnitude to share one axis honestly. "
                "The values are in the table."
            )
    elif chart_plan.get("reason"):
        st.caption(chart_plan["reason"])

    # Always visible — see constraint 4.
    st.dataframe(frame, use_container_width=True, hide_index=True, key=f"{key}-table")

    meta = [f"{query.get('row_count', len(rows)):,} rows"]
    if query.get("tables"):
        meta.append("from " + ", ".join(f"`{t}`" for t in query["tables"]))
    st.caption(" · ".join(meta))

    if query.get("truncated"):
        st.info(
            f"Showing the first {query.get('row_limit')} rows — the query matched "
            "more. This is a partial answer; ask for an aggregate or a top-N for "
            "a complete one."
        )


def render_exchange(exchange: dict) -> None:
    """One question and everything that came back for it."""
    st.markdown(f"#### {exchange['question']}")
    exchange_id = exchange.get("id", 0)

    if exchange.get("failed"):
        st.error(exchange["answer"])
    else:
        st.markdown(exchange["answer"])

    queries = exchange.get("queries") or []
    if queries:
        st.divider()
        for position, query in enumerate(queries, start=1):
            render_query(
                position if len(queries) > 1 else 0,
                query,
                key=f"q{exchange_id}-{position}",
            )

    footer = []
    if exchange.get("tokens"):
        estimated = "" if exchange.get("tokens_measured", True) else " (estimated)"
        footer.append(f"{exchange['tokens']:,} tokens{estimated}")
    if exchange.get("steps"):
        footer.append(f"{exchange['steps']} agent steps")
    if footer:
        st.caption(" · ".join(footer))


def main() -> None:
    if "history" not in st.session_state:
        st.session_state.history = []
    if "questions" not in st.session_state:
        st.session_state.questions = 0
    if "pending" not in st.session_state:
        st.session_state.pending = None
    if "next_id" not in st.session_state:
        # Monotonic, never reused, so a widget key never refers to two different
        # answers over the life of the session.
        st.session_state.next_id = 1

    st.title("Weather Warehouse SQL Explorer")
    st.markdown(
        "Ask a question in plain English. It is turned into PostgreSQL, run "
        "against a **9.2 million row** weather warehouse, and the query is shown "
        "alongside the answer so you can check the work."
    )

    with st.expander("How this works"):
        st.markdown(
            """
**Gemini 3.7 Flash** (via **LangChain**) is given three tools by a
**Model Context Protocol** server, and no database credentials at all:

| Tool | What the model does with it |
|---|---|
| `list_schema` | discovers the ten readable tables |
| `describe_table` | reads column types and **units** out of the database itself |
| `run_sql` | runs one read-only `SELECT` |

The web app cannot reach Postgres directly. Everything goes through the MCP
server, which connects as a role holding `SELECT` on ten tables and nothing
else, inside a read-only transaction, with a 30-second statement timeout. Every
generated statement is parsed before execution and rejected unless it is a
single read-only query over an approved table.

Two databases are behind it — a **GHCNh** historical archive (9.2M hourly
observations, 112 US stations, 2019 to now) and **Apple WeatherKit** forecasts.
`postgres_fdw` presents the forecast tables inside the historical database, so a
single query can compare a forecast against that station's own history.
"""
        )

    usage = budget.status()
    if not usage["healthy"]:
        st.error(
            "The usage ledger is unreadable, so questions are paused. "
            "This is a server-side problem, not something you did."
        )

    st.markdown("###### Try one of these")
    columns = st.columns(3)
    for position, example in enumerate(EXAMPLES):
        with columns[position % 3]:
            if st.button(example, key=f"ex{position}", use_container_width=True):
                st.session_state.pending = example

    typed = st.chat_input("Ask about temperature, wind, pressure, forecasts…")
    if typed:
        st.session_state.pending = typed

    question = st.session_state.pending
    if question:
        st.session_state.pending = None
        try:
            active_agent, _ = get_agent()
        except agent_module.SetupError as exc:
            st.error(str(exc))
            active_agent = None
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not start the agent: {exc}")
            active_agent = None

        if active_agent is not None:
            with st.spinner("Writing SQL and querying the warehouse…"):
                try:
                    result = agent_module.ask(
                        active_agent, question, st.session_state.questions
                    )
                    st.session_state.questions += 1
                    st.session_state.history.insert(
                        0,
                        {
                            "id": st.session_state.next_id,
                            "question": question,
                            **result,
                        },
                    )
                    st.session_state.next_id += 1
                except budget.BudgetExceeded as exc:
                    st.session_state.history.insert(
                        0,
                        {
                            "id": st.session_state.next_id,
                            "question": question,
                            "answer": str(exc),
                            "failed": True,
                            "queries": [],
                        },
                    )
                    st.session_state.next_id += 1
                except Exception as exc:  # noqa: BLE001
                    st.session_state.questions += 1
                    st.session_state.history.insert(
                        0,
                        {
                            "id": st.session_state.next_id,
                            "question": question,
                            "answer": (
                                "That question could not be answered: "
                                f"{exc}\n\nTry rephrasing it, or narrowing the "
                                "time range."
                            ),
                            "failed": True,
                            "queries": [],
                        },
                    )
                    st.session_state.next_id += 1

    for position, exchange in enumerate(st.session_state.history):
        if position:
            st.divider()
        render_exchange(exchange)

    st.divider()
    remaining_session = max(
        0, budget.MAX_QUESTIONS_PER_SESSION - st.session_state.questions
    )
    st.caption(
        f"{remaining_session} of {budget.MAX_QUESTIONS_PER_SESSION} questions left "
        "in this session · demo capacity resets at 00:00 UTC · "
        "the database is read-only"
    )


if __name__ == "__main__":
    main()

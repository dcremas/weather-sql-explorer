"""Weather Warehouse SQL Explorer — ask in English, see the SQL, see the data.

    ./run.sh                      # local, needs the SSH tunnel + the MCP server
    streamlit run app.py          # if you have already sorted the environment

SIX CONSTRAINTS THAT BREAK THIS APP IF REMOVED
----------------------------------------------
1. `st.plotly_chart(..., theme=None)` on EVERY chart. The default
   `theme="streamlit"` silently repaints figures in Streamlit's own palette and
   discards every colour decision in theme.py -- including the CVD validation
   those colours were chosen to pass. The chart still looks fine, which is why
   this keeps getting reintroduced. `.streamlit/config.toml` deliberately does
   NOT set `chartCategoricalColors`, so a chart that loses `theme=None` looks
   wrong immediately instead of looking nearly right.

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
   theme.py) as well as the honest view of what the query returned. Tabs are
   fine -- the chart and its table are always in the same one -- but a chart with
   its table folded away is not.

5. `st.chat_input` is called INSIDE the Ask tab, not from the main body. This
   looks backwards -- the main body is what pins the box to the bottom of the
   window, which is nicer -- and it is deliberate. Calling it from the main body
   makes Streamlit wrap the whole main area in `stAppScrollToBottomContainer`
   and scroll it to the bottom on every rerun. The masthead and the four-step
   strip then sit above the viewport from the first paint, painted over by the
   56px `stHeader` overlay: the page title is legibly half-erased, and the one
   explanation every visitor is supposed to see is scrolled off. That is the
   whole redesign undone for the sake of a pinned input, so the pin was the
   thing that went. Verified by measurement, not by eye -- see the note on top
   padding in ui.py.

6. History is PREPENDED, so the newest answer is first. That follows from 5: the
   question box now sits at the top of the Ask tab, so the answer belongs
   directly beneath it, where the visitor is already looking. If the input ever
   moves back to the bottom of the page, this has to flip with it.

WHAT IS ON THE PAGE AND WHY
---------------------------
The generated SQL is displayed as a first-class part of the answer, not hidden in
a debug panel. The point of the app is that the question became a query; a
visitor who cannot see the query has to take the number on faith, and a wrong
answer is indistinguishable from a right one.

THE PAGE IS BUILT FOR SOMEONE WHO HAS NOT MET TEXT-TO-SQL
---------------------------------------------------------
Most visitors arrive from a portfolio link and have never seen a language model
write SQL. They get the idea in three widening steps, and can stop at any of
them:

  the hero paragraph      one sentence: English in, SQL and rows out
  the four-step strip     the whole pipeline, unfolded, above the question box
  the "How it works" tab  what text-to-SQL is, how it fails, how to check it

Only the third is behind a click, and nothing on the first two is collapsible.
An explanation a newcomer has to go looking for is an explanation they will not
read, so the concept lives on the landing view and the detail lives in the tab.

`guide.py` holds the long explanation, `data_model.py` the schema reference, and
`ui.py` the stylesheet and the diagram primitives. This file is now layout and
the ask/answer loop, and should stay that size.
"""

from __future__ import annotations

import streamlit as st

import agent as agent_module
import budget
import charts
import data_model
import guide
import ui

st.set_page_config(
    page_title="Weather Warehouse SQL Explorer",
    page_icon="🌤️",
    layout="wide",
)

# This is a public demo linked from a portfolio, so it has to answer two
# questions a visitor will have within seconds of arriving: where is the code,
# and who made it. Both live in the sidebar rather than the masthead -- the
# question box should stay the loudest thing on the page.
REPO_URL = "https://github.com/dcremas/weather-sql-explorer"
SITE_URL = "https://www.dustincremascoli.com"

# The masthead facts. Deliberately approximate and hardcoded: they render on
# every page load, and making the landing view wait on -- or fail with -- a
# 9-million-row count would be a bad trade for two significant figures. The
# exact, live numbers are on the Data model tab, which is where a visitor goes
# when they want precision.
HERO_CHIPS = [
    ("9.2M", "hourly observations"),
    ("112", "US weather stations"),
    ("2019 → today", "plus 2 weeks of forecast"),
    ("read-only", "no query can change anything"),
]

# The concept, unfolded, above the question box. See constraint-adjacent note in
# the module docstring: this is the one explanation every visitor sees.
STEPS = [
    (
        "You ask in English",
        "No SQL required. Name a place, a measure and a time range.",
    ),
    (
        "A model writes the query",
        "Gemini reads the table descriptions, then writes one PostgreSQL SELECT.",
    ),
    (
        "The database runs it",
        "Checked, then executed read-only against 9.2M rows. 30-second ceiling.",
    ),
    (
        "You see the query too",
        "Answer, chart, rows — and the SQL, so you can check the work.",
    ),
]

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


# --------------------------------------------------------------------------- #
# Rendering one answer
# --------------------------------------------------------------------------- #


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
    label = f"Query {index}" if index else "The SQL that answered it"

    if query.get("error"):
        st.markdown(f"**{label}** — refused")
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
        st.plotly_chart(figure, width="stretch", theme=None, key=f"{key}-chart")
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
    st.dataframe(frame, width="stretch", hide_index=True, key=f"{key}-table")

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
        if exchange.get("detail"):
            with st.expander("What the server actually reported"):
                st.code(exchange["detail"], language="text")
    else:
        st.markdown(exchange["answer"])

    queries = exchange.get("queries") or []
    if queries:
        st.write("")
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


# --------------------------------------------------------------------------- #
# Page furniture
# --------------------------------------------------------------------------- #


def render_sidebar(usage: dict) -> None:
    """Standing context: what this is, what it costs, and where the code is.

    The sidebar carries the things a visitor needs available but not in the way:
    a one-paragraph orientation, the remaining budget, and the two links. None of
    it competes with the question box, and none of it is the only place any of
    this information appears.
    """
    with st.sidebar:
        st.markdown("### 🌤️ SQL Explorer")
        st.markdown(
            "A question typed in English becomes a **PostgreSQL** query, runs "
            "against a weather warehouse, and comes back with the query "
            "attached."
        )
        st.markdown(
            "**New to this?** The *How it works* tab explains what the model is "
            "doing, and where it can go wrong. The *Data model* tab shows every "
            "table you can ask about — both are free to read."
        )

        st.divider()
        st.markdown("**Under the hood**")
        st.markdown(
            f"""
- Writes the SQL — `{agent_module.MODEL}`
- Runs the SQL — a `SELECT`-only Postgres role
- Between them — an MCP server offering exactly three tools
- The web app itself holds **no database password**
"""
        )

        st.divider()
        st.markdown("**This session**")
        asked = st.session_state.questions
        allowed = budget.MAX_QUESTIONS_PER_SESSION
        st.progress(min(1.0, asked / allowed) if allowed else 0.0)
        st.caption(
            f"{max(0, allowed - asked)} of {allowed} questions left · the shared "
            "daily budget resets at 00:00 UTC"
        )
        if not usage["healthy"]:
            st.caption("⚠️ The usage ledger is unreadable, so questions are paused.")

        st.divider()
        st.markdown(
            f"[Source on GitHub]({REPO_URL}) · [dustincremascoli.com]({SITE_URL})"
        )
        st.caption(
            "The repo holds both halves: this app, and the MCP server that "
            "executes its SQL."
        )


def render_examples(has_history: bool) -> None:
    """The six starter questions.

    Kept as buttons rather than a dropdown because the label IS the teaching:
    six examples in view tell a newcomer the shape of question this warehouse
    answers faster than any instruction. Once there is an answer on the page they
    fold away -- at that point the visitor knows what to type, and the six
    buttons are just pushing their answer down the screen.
    """
    if has_history:
        with st.expander("Try another example question"):
            _example_grid()
        return

    st.markdown("##### Not sure what to ask? Start with one of these")
    _example_grid()


def _example_grid() -> None:
    # Keyed container: the stylesheet in ui.py left-aligns these labels via
    # `.st-key-sqlx-examples`, which is a documented hook rather than an
    # internal class name.
    with st.container(key="sqlx-examples"):
        columns = st.columns(3, gap="small")
        for position, example in enumerate(EXAMPLES):
            with columns[position % 3]:
                if st.button(example, key=f"ex{position}", width="stretch"):
                    st.session_state.pending = example


# --------------------------------------------------------------------------- #
# The ask/answer loop
# --------------------------------------------------------------------------- #


# Failure shapes worth translating. A visitor who has never met text-to-SQL
# cannot act on "Recursion limit of 14 reached without hitting a stop condition.
# You can increase the limit by setting the recursion_limit config key" -- and
# that message also puts a LangChain docs link on a public page, which reads as
# an unhandled crash rather than a cap doing its job. The raw text is still
# offered, folded away, because it is the thing an operator needs.
def explain_failure(exc: Exception) -> tuple[str, str | None]:
    """Turn one exception into (what to tell the visitor, the raw detail)."""
    raw = str(exc)
    lowered = raw.lower()

    if "recursion limit" in lowered or "graph_recursion_limit" in lowered:
        return (
            f"The model used up all {budget.MAX_AGENT_STEPS} steps this demo "
            "allows for one question without arriving at an answer — usually a "
            "sign it kept rewriting a query it could not get right.\n\nAsking "
            "for something narrower often works: one station instead of all "
            "112, or a named month instead of a full history.",
            raw,
        )
    if "timeout" in lowered or isinstance(exc, TimeoutError):
        return (
            "That question ran past the time limit. Queries are capped at 30 "
            "seconds each and a whole question at three minutes, so a narrower "
            "time range or an aggregate instead of raw rows should get "
            "through.",
            raw,
        )
    return (
        "That question could not be answered. Try rephrasing it, or narrowing "
        "the time range.",
        raw,
    )


def answer_pending() -> None:
    """Run the one question waiting in session state, if there is one.

    Called from inside the Ask tab so the spinner appears where the answer will,
    rather than above the masthead. Every exit path appends to history, including
    the failures: an error the visitor cannot see is indistinguishable from the
    app hanging.
    """
    question = st.session_state.pending
    if not question:
        return
    st.session_state.pending = None

    try:
        active_agent, _ = get_agent()
    except agent_module.SetupError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not start the agent: {exc}")
        return

    def remember(payload: dict) -> None:
        # Prepended: the question box is at the top of this tab, so the newest
        # answer belongs directly under it. See constraint 6.
        st.session_state.history.insert(
            0, {"id": st.session_state.next_id, "question": question, **payload}
        )
        st.session_state.next_id += 1

    with st.spinner("Reading the schema, writing SQL, querying the warehouse…"):
        try:
            result = agent_module.ask(
                active_agent, question, st.session_state.questions
            )
            st.session_state.questions += 1
            remember(result)
        except budget.BudgetExceeded as exc:
            # Not charged and not counted: check_can_spend refused before the
            # model ran, so nothing was spent. See constraint 3.
            remember({"answer": str(exc), "failed": True, "queries": []})
        except Exception as exc:  # noqa: BLE001
            st.session_state.questions += 1
            message, detail = explain_failure(exc)
            remember(
                {
                    "answer": message,
                    "detail": detail,
                    "failed": True,
                    "queries": [],
                }
            )


def render_ask() -> None:
    """The Ask tab: the question box, the examples, then the answers.

    The input is read here rather than in `main` -- see constraint 5 -- and it is
    read BEFORE `answer_pending`, because what it returns is what that consumes.
    """
    typed = st.chat_input("Ask about temperature, wind, pressure, forecasts…")
    if typed:
        st.session_state.pending = typed

    render_examples(bool(st.session_state.history))
    answer_pending()

    if not st.session_state.history:
        st.write("")
        ui.callout(
            "What you will get back",
            "An answer in a sentence or two, a chart when the shape of the "
            "result warrants one, the full table of rows, and the "
            "<b>PostgreSQL that produced them</b>.",
            "The query is shown because a language model can write valid SQL "
            "that answers the wrong question, and the number it returns looks "
            "identical either way. Reading the query is how you tell.",
        )
        return

    for position, exchange in enumerate(st.session_state.history):
        if position:
            st.divider()
        render_exchange(exchange)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


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

    # No theme flag anywhere in the presentation layer. ui.py derives every
    # colour from the theme's own text colour in the browser, so a visitor
    # toggling Light/Dark gets correct colours immediately instead of on the next
    # rerun -- which is what used to happen, and it rendered near-black text on a
    # near-black background in the meantime. See the ui.py module docstring.
    ui.inject()

    usage = budget.status()

    ui.hero(
        "Weather Warehouse SQL Explorer",
        "Ask a weather question in plain English. A language model turns it into "
        "<strong>PostgreSQL</strong>, a read-only server runs it against "
        "<strong>9.2 million hourly observations</strong>, and the query comes "
        "back alongside the answer — so you can check the work instead of "
        "trusting it.",
        HERO_CHIPS,
    )
    ui.steps(STEPS)

    if not usage["healthy"]:
        st.error(
            "The usage ledger is unreadable, so questions are paused. "
            "This is a server-side problem, not something you did."
        )

    ask_tab, how_tab, data_tab = st.tabs(
        ["Ask a question", "How it works", "The data model"]
    )
    # The sidebar is drawn AFTER the tabs, and in a `finally` so it survives a
    # failure in one of them.
    #
    # Streamlit places sidebar content by the `with st.sidebar:` block, not by
    # where the call sits in the script, so this costs nothing visually and fixes
    # a real bug: `st.session_state.questions` is incremented inside the Ask tab,
    # so a sidebar rendered before the tabs shows the count from BEFORE the
    # question that was just answered. It read "12 of 12 questions left" on a
    # page that was already displaying an answer.
    try:
        with ask_tab:
            render_ask()
        with how_tab:
            guide.render()
        with data_tab:
            data_model.render()
    finally:
        render_sidebar(usage)


if __name__ == "__main__":
    main()

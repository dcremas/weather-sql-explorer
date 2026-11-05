"""Ask Weather (the weather SQL explorer) — ask in English, see the SQL, see the data.

    ./run.sh                      # local, needs the SSH tunnel + the MCP server
    streamlit run app.py          # if you have already sorted the environment

THE LAYOUT IS ASK FOOTBALL'S (2026-11-05)
-----------------------------------------
One 960px column of cards: the header, the Ask card with a live scope line and the
budget, example chips, one card per answer, then "About the data" and the estate
footer. Ask Football (~/projects/pbp/explorer/app.py) got there first, and the two
apps are the same idea pointed at two warehouses, so they now read as one. style.py
and estate.py are copies of Ask Football's; this file follows its app.py section by
section. Until 2026-11-05 this page had a sidebar, a hero, a four-step strip and
three tabs (Ask / How it works / The data model); that explanation now lives,
shorter, in the About the data card.

FOUR CONSTRAINTS THAT BREAK THIS APP IF REMOVED
-----------------------------------------------
1. `st.plotly_chart(..., theme=None)` on EVERY chart. The default
   `theme="streamlit"` silently repaints figures in Streamlit's own palette and
   discards every colour decision in theme.py -- including the CVD validation
   those colours were chosen to pass. The chart still looks fine, which is why
   this keeps getting reintroduced. `.streamlit/config.toml` deliberately does
   NOT set `chartCategoricalColors`, so a chart that loses `theme=None` looks
   wrong immediately instead of looking nearly right.

2. The agent is built inside `@st.cache_resource`. Streamlit re-runs this whole
   script on every interaction; without the cache each question would rebuild the
   MCP client, re-discover tools over HTTP, and abandon the previous connection.

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

import html
import re
import time
from datetime import datetime

import pandas as pd
import streamlit as st

import agent as agent_module
import budget
import charts
import estate
import style

st.set_page_config(
    page_title="Ask Weather — Dustin Cremascoli",
    page_icon="🌤️",
    layout="wide",
)
style.inject()

REPO_URL = "https://github.com/dcremas/weather-sql-explorer"

EXAMPLES = [
    "Which 5 stations are forecast furthest above their own August average temperature?",
    "What were the 10 hottest hours ever recorded, and where?",
    "How has the average August temperature changed each year since 2019?",
    "Which states have the windiest weather on average?",
    "Show the daily average temperature at Denver for the last 30 days of data.",
    "Which stations show the sharpest 24-hour pressure drops?",
]

# The About the data card's "Good to know": the few caveats that change how an
# answer should be read, in plain words, for a visitor rather than the model. The
# model's own version is the table and column comments it reads through
# describe_table, which stay under "For SQL readers".
GOOD_TO_KNOW = [
    "<b>The newest observations are usually a day or more behind.</b> NOAA publishes "
    "them in batches, so “the last 30 days of data” ends at the latest batch, not today.",
    "<b>The current year and month are still filling in.</b> Trends compare complete "
    "years; a partial one is labelled as such.",
    "<b>Times are local standard time at each station,</b> all year round, so summer "
    "hours read an hour earlier than the clock on the wall.",
    "<b>Units:</b> temperature and dew point in °F, wind in mph, pressure in inHg, "
    "rain in inches, visibility in miles.",
    "<b>Rainfall is the weakest measure.</b> A gauge outage is stored as no rain, so a "
    "few station-years read drier than they were.",
    "<b>112 stations, mostly airports.</b> A city's weather here is its airport "
    "station's, and a place without a station cannot be asked about.",
    "<b>The forecast is a rolling window</b> from Apple WeatherKit, about the next two "
    "weeks plus the days just gone. Each hour holds its latest forecast, not the "
    "earlier ones.",
    "<b>Not in the data:</b> snowfall, storms and warnings, or official climate "
    "normals.",
]


@st.cache_resource(show_spinner=False)
def get_agent():
    """Build the agent once per process. See constraint 2 in the module docstring."""
    return agent_module.build_agent()


# --------------------------------------------------------------------------- #
# Reads that need no model -- free, cached, and still working when the model is not
# --------------------------------------------------------------------------- #


@st.cache_data(ttl=300, show_spinner=False)
def _schema() -> list[dict]:
    """The readable tables, straight from the server."""
    payload = agent_module.call_tool("list_schema")
    return payload.get("tables", []) if isinstance(payload, dict) else []


@st.cache_data(ttl=3600, show_spinner=False)
def _coverage() -> dict | None:
    """Observations, stations and the newest one -- the numbers the scope line states.

    Read live because a hand-typed count goes stale: the old masthead said 9.2M
    for months after it passed 9.4M. None when the server is down, and the page
    then states the scope in words only. An hour's cache, not Ask Football's ten
    minutes: the count takes about two seconds, and new observations land every
    one to four days.
    """
    try:
        payload = agent_module.call_tool("data_coverage")
    except Exception:  # noqa: BLE001 - the scope line is decoration, not the page
        return None
    if not isinstance(payload, dict) or "observations" not in payload:
        return None
    return payload["observations"]


def _scope_html() -> str:
    bits = ["<b>Weather warehouse</b>", "Hourly observations from US weather stations"]
    cov = _coverage()
    if cov:
        bits = [
            "<b>Weather warehouse</b>",
            f"{cov.get('station_count', 112)} US stations",
            f"{(cov.get('row_count') or 0) / 1e6:.2f}M hourly observations",
        ]
        if cov.get("latest"):
            day = datetime.fromisoformat(cov["latest"])
            bits.append(f"2019 through {day:%b} {day.day}, {day.year}")
    bits.append("plus a two-week forecast")
    return f'<p class="pp-scope">{" · ".join(bits)}</p>'


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #

st.session_state.setdefault("questions", 0)
st.session_state.setdefault("history", [])
st.session_state.setdefault("pending", None)
# Monotonic, never reused, so a widget key never refers to two different answers.
st.session_state.setdefault("next_id", 1)


def _pick_example() -> None:
    """A chip click asks its question. The chip is then cleared so it can be asked again."""
    chosen = st.session_state.get("examples")
    if chosen:
        st.session_state.pending = chosen
    st.session_state["examples"] = None


status = budget.status()
left_q = max(0, budget.MAX_QUESTIONS_PER_SESSION - st.session_state.questions)
remaining_pct = status["remaining"] / max(status["tokens_limit"], 1) * 100
paused = agent_module.model_status()
can_ask = status.get("healthy") and status["remaining"] > 0 and left_q > 0 and not paused

_UNAVAILABLE = {
    "billing": (
        "<b>Answers are paused.</b> The language model behind this page is out of "
        "credit. The data notes below still work, and the warehouse itself is "
        "unaffected."
    ),
    "rate": (
        "<b>The language model is busy.</b> It refused the last request as too many "
        "at once. Try again in a minute."
    ),
}


# --------------------------------------------------------------------------- #
# Header and the ask card
# --------------------------------------------------------------------------- #

style.header(
    "Ask about the weather at 112 US stations since 2019, or the next two weeks' "
    "forecast. A language model writes the SQL, runs it read-only, and shows you the "
    "query."
)

with st.container(key="card-ask"):
    st.html('<p class="pp-eyebrow">Ask a question</p>')
    st.html(_scope_html())

    with st.form("ask", border=False, clear_on_submit=False):
        question = st.text_area(
            "Your question",
            max_chars=400,
            label_visibility="collapsed",
            height="content",
            placeholder="Ask about temperature, wind, pressure, rain or the forecast…",
        )
        with st.container(horizontal=True, vertical_alignment="center"):
            if paused:
                st.html(
                    '<span class="pp-status" data-state="paused">Answers paused</span>',
                    width="content",
                )
            elif not status.get("healthy"):
                st.html(
                    '<span class="pp-status" data-state="paused">Usage ledger '
                    "unreadable — questions paused</span>",
                    width="content",
                )
            else:
                st.html(
                    f'<span class="pp-status"><b>{left_q}</b> questions left · '
                    f"<b>{remaining_pct:.0f}%</b> of today's model budget</span>",
                    width="content",
                )
            st.space("stretch")
            submitted = st.form_submit_button("Ask", type="primary", disabled=not can_ask)

if submitted and question and question.strip():
    st.session_state.pending = question.strip()

st.html('<p class="pp-label">Try one of these</p>')
st.pills(
    "Try one of these",
    EXAMPLES,
    key="examples",
    disabled=not can_ask,
    on_change=_pick_example,
    label_visibility="collapsed",
)


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #


def explain_failure(exc: Exception) -> str:
    """What to tell the visitor about one failed question, as trusted markup.

    A visitor who has never met text-to-SQL cannot act on "Recursion limit of 28
    reached without hitting a stop condition", and that message also puts a
    LangChain docs link on a public page. The raw text is offered separately,
    folded away, because it is the thing an operator needs.
    """
    lowered = str(exc).lower()
    if "recursion limit" in lowered or "graph_recursion_limit" in lowered:
        return (
            f"<b>The model used up all {budget.MAX_AGENT_STEPS} steps</b> this demo "
            "allows for one question without arriving at an answer — usually a sign "
            "it kept rewriting a query it could not get right. Asking for something "
            "narrower often works: one station instead of all 112, or a named month "
            "instead of a full history."
        )
    if "timeout" in lowered or isinstance(exc, TimeoutError):
        return (
            "<b>That question ran past the time limit.</b> Queries are capped at 30 "
            "seconds each and a whole question at three minutes, so a narrower time "
            "range or an aggregate instead of raw rows should get through."
        )
    # The turn was still charged; see agent.ask. Saying so keeps the counter honest
    # rather than looking like a free failure.
    return (
        "<b>That question could not be answered.</b> Try rephrasing it, or narrowing "
        "the time range. It was still counted against the daily budget, because it "
        "still spent tokens."
    )


if paused:
    style.notice(_UNAVAILABLE[paused], "warn")
elif not status.get("healthy"):
    style.notice(
        "<b>Questions are paused.</b> The usage ledger is unreadable, and the page "
        "fails closed on purpose rather than spend without counting. "
        f"({html.escape(str(status.get('error', 'unknown')))})",
        "error",
    )

pending = st.session_state.pending
st.session_state.pending = None
if pending:
    try:
        with st.spinner("Asking the weather warehouse… this usually takes 10–40 seconds"):
            t0 = time.time()
            active_agent, _ = get_agent()
            result = agent_module.ask(
                active_agent,
                pending,
                st.session_state.questions,
                history=st.session_state.history,
            )
        result.update(
            elapsed=time.time() - t0, question=pending, id=st.session_state.next_id
        )
        st.session_state.next_id += 1
        st.session_state.questions += 1
        st.session_state.history.insert(0, result)
        # The status line is rendered ABOVE this block, so without a rerun it shows
        # the state from before the question that just ran -- a counter that reads
        # 12 after you have asked one. Rerunning redraws it with the ledger's numbers.
        st.rerun()
    except agent_module.ModelUnavailable:
        # model_status() now reports the outage, so the rerun draws the paused status
        # line and the notice from the top of the page. Not counted: nothing was bought.
        st.rerun()
    except budget.BudgetExceeded as exc:
        # Not charged and not counted: check_can_spend refused before the model ran.
        style.notice(html.escape(str(exc)), "warn")
    except agent_module.SetupError as exc:
        style.notice(
            "<b>The warehouse is unreachable right now.</b> Please try again shortly.",
            "error",
        )
        with st.expander("Details for the operator"):
            st.code(str(exc), language=None)
    except Exception as exc:  # noqa: BLE001 - the page must survive a bad question
        st.session_state.questions += 1
        style.notice(explain_failure(exc), "error")
        with st.expander("Details"):
            st.code(f"{type(exc).__name__}: {exc}", language=None)


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #


def _md(text: str) -> str:
    # A bare `$` opens LaTeX in st.markdown, so "$5 ... $10" renders as math.
    return text.replace("$", r"\$")


# Integers that are labels, not quantities: "2,019" is not a year.
_PLAIN_INT = ("year", "month", "day", "hour", "week", "quarter", "decade", "doy")


def _column_config(df: pd.DataFrame) -> dict:
    """Readable headers and number formats for a result table.

    The table is the evidence under every answer, so it should read like one:
    "Avg temp (°F)" over 75.2, not avg_temp_f; 8,712 hours, not 8712; years
    without a thousands separator; pressure to two decimals, because 29.92 and
    30.0 inHg are different weather. The data itself is untouched -- sorting and
    the CSV download still see the raw values.
    """
    if df.columns.duplicated().any():
        return {}
    cfg = {}
    for col in df.columns:
        name, s = str(col).lower(), df[col]
        title = charts.label(col)
        if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            cfg[col] = st.column_config.Column(title)
            continue
        values = s.dropna()
        whole = values.empty or bool((values == values.round()).all())
        if name in _PLAIN_INT or name.endswith(("_id", "_year")):
            fmt = "%d"
        elif name.endswith("_pct") or name == "pct":
            fmt = "%.1f%%"
        elif name.endswith(("_inhg", "_in")):
            fmt = "%.2f"
        elif whole:
            fmt = "localized"
        elif bool(((values * 10).round(6) % 1 == 0).all()):
            # Already one decimal, as the prompt asks: "0.5", not Ask Football's "0.50".
            fmt = "%.1f"
        else:
            fmt = "%.2f" if values.abs().max() < 10 else "%.1f"
        cfg[col] = st.column_config.NumberColumn(title, format=fmt)
    return cfg


def _result(query: dict, primary: bool, key: str) -> None:
    """One run_sql call: its chart, its SQL, then its rows or the reason it has none.

    `key` must be unique across the page AND stable across reruns: two answers
    about the same table produce near-identical figures, and Streamlit dies with
    StreamlitDuplicateElementId when two charts share an id. It is built from the
    answer's own id, not its position, because history is prepended.
    """
    if query.get("error"):
        st.html('<p class="pp-label">SQL</p>')
        st.code(query.get("requested_sql") or "", language="sql", wrap_lines=True)
        # Shown rather than hidden: a refused query is the guard working, and
        # watching it refuse is more informative than a generic failure.
        style.notice("<b>Refused or failed:</b> " + html.escape(str(query["error"])), "warn")
        return

    columns = query.get("columns") or []
    rows = query.get("rows") or []
    frame = charts.build_frame(columns, rows) if columns else None

    if primary and frame is not None:
        chart_plan = charts.plan(frame)
        if chart_plan["chartable"]:
            # theme=None is load-bearing -- see constraint 1.
            st.plotly_chart(
                charts.figure(frame, chart_plan),
                width="stretch",
                theme=None,
                key=f"{key}-chart",
            )
            if chart_plan["dropped"]:
                st.html(
                    '<p class="pp-scope">Not charted: '
                    + ", ".join(html.escape(charts.label(c)) for c in chart_plan["dropped"])
                    + " — a different unit or scale from the first measure, so not drawn "
                    "on its axis. The values are in the table.</p>"
                )

    st.html('<p class="pp-label">SQL</p>')
    st.code(
        query.get("sql") or query.get("requested_sql") or "",
        language="sql",
        wrap_lines=True,
    )

    if query.get("plan"):
        with st.expander("Query plan (validated, not executed)"):
            st.code("\n".join(query["plan"]), language="text")
        return
    if frame is None:
        style.notice("No result set returned.")
        return

    count = query.get("row_count", len(rows))
    bits = [f"{count:,} row" + ("" if count == 1 else "s")]
    if query.get("tables"):
        bits.append("read " + ", ".join(query["tables"]))
    st.html(f'<p class="pp-label">Result · {html.escape(" · ".join(bits))}</p>')
    if query.get("truncated") and query.get("limit_source") != "caller":
        style.notice(
            f"<b>Showing the first {query.get('row_limit')} rows</b> — the query matched "
            "more, so this is a partial answer. Ask for an aggregate or a top-N for a "
            "complete one.",
            "warn",
        )
    # Always visible -- see constraint 4. Every row of a ranking or a yearly trend on
    # screen at once; 35 px is the grid's row height, and past twenty rows the table
    # scrolls, as a long result should.
    st.dataframe(
        frame,
        hide_index=True,
        height=35 * (min(len(frame), 20) + 1) + 3,
        column_config=_column_config(frame),
        key=f"{key}-table",
    )


# The prompt ends every answer with "Next: q1 | q2 | q3". Markdown emphasis around the
# label is tolerated, because models add it.
_NEXT = re.compile(r"^[ \t>*_-]*next[*_]*\s*[:：][*_]*\s*(.+?)\s*$", re.I | re.M)


def _split_next(answer: str) -> tuple[str, list[str]]:
    """The answer without its "Next:" line, and the follow-up questions that line offered.

    An answer without one comes back unchanged with no follow-ups: they are an offer,
    not part of the finding, so a model that skips them costs the reader nothing.
    """
    found = list(_NEXT.finditer(answer or ""))
    if not found:
        return answer, []
    last = found[-1]
    asks = [q.strip(" *_`\"'“”") for q in last.group(1).split("|")]
    asks = list(dict.fromkeys(q for q in asks if 8 <= len(q) <= 160))[:3]
    return (answer[: last.start()] + answer[last.end() :]).rstrip(), asks


def _pick_next(answer_id: int) -> None:
    """A follow-up click asks it, carrying the conversation as any typed follow-up does."""
    key = f"next-{answer_id}"
    if st.session_state.get(key):
        st.session_state.pending = st.session_state[key]
    st.session_state[key] = None


def _new_conversation() -> None:
    # Clears the answers AND the follow-up context, which are the same list -- see
    # agent._prior_turns. `questions` is deliberately left alone: starting over is
    # not a way to earn more questions.
    st.session_state.history = []


if st.session_state.history:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.html(
            '<p class="pp-scope">Follow-ups can refer back: each question carries your '
            f"last {agent_module.CONTEXT_EXCHANGES} as context.</p>",
            width="stretch",
        )
        st.button("New conversation", on_click=_new_conversation)

for item in st.session_state.history:
    with st.container(key=f"card-answer-{item['id']}"):
        charged = f"<b>{item['tokens']:,}</b> tokens"
        if not item.get("tokens_measured", True):
            charged += " (estimated)"
        st.html(
            '<div style="display:flex;justify-content:space-between;align-items:center;'
            'gap:12px;flex-wrap:wrap">'
            '<p class="pp-eyebrow">Question</p>'
            f'<p class="pp-meta"><b>{item.get("elapsed", 0):.1f}s</b> · {charged}</p></div>'
            f'<p class="pp-question">{html.escape(item["question"])}</p>'
        )
        body, follow_ups = _split_next(item.get("answer") or "")
        if body and body != agent_module.NO_ANSWER:
            st.markdown(_md(body))
        else:
            style.notice("The model returned no written answer. Its queries are below.")

        # The answer rests on the LAST query: earlier ones are attempts that errored,
        # were refused or were rewritten.
        queries = item.get("queries") or []
        if queries:
            _result(queries[-1], primary=True, key=f"q{item['id']}-{len(queries)}")
        if len(queries) > 1:
            with st.expander(f"Earlier attempts ({len(queries) - 1})"):
                for position, query in enumerate(queries[:-1], start=1):
                    _result(query, primary=False, key=f"q{item['id']}-{position}")

        if follow_ups:
            st.html('<p class="pp-label">Ask next</p>')
            st.pills(
                "Ask next",
                follow_ups,
                key=f"next-{item['id']}",
                disabled=not can_ask,
                on_change=_pick_next,
                args=(item["id"],),
                label_visibility="collapsed",
            )


# --------------------------------------------------------------------------- #
# About the data -- no model involved, so it costs nothing and works when the model
# is down
# --------------------------------------------------------------------------- #


def _prose(text: str) -> str:
    """Escape, then let `backticks` read as code."""
    text = html.escape(text or "")
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


# What a visitor can ask about, one tile per subject. Each names the table whose
# size it shows, or a fixed size where the table cannot report one (the forecast
# tables are foreign tables, and the planner has no estimate for them).
_SUBJECTS = (
    ("observations", "Observations", None,
     "Hourly temperature, dew point, wind, pressure, rain, visibility and cloud "
     "ceiling at every station since 2019."),
    ("hourlyforecasts", "Forecasts", "2 weeks",
     "Apple WeatherKit's hour-by-hour forecast for the same stations, in the same "
     "units, so it compares directly with history."),
    ("obs_baro_impact", "Pressure changes", None,
     "How fast sea-level pressure rose or fell over 3, 6 and 24 hours — the signal "
     "of an incoming front."),
    ("hf_baro_impact", "Forecast pressure", "2 weeks",
     "The same 3, 6 and 24-hour pressure changes, over the forecast window."),
    ("loc_subset", "Stations", "112",
     "Each station's name, state, region and coordinates — most are airports."),
    ("regions", "Regions", None,
     "US states rolled up into census regions and sub-regions, for comparing "
     "parts of the country."),
)
# The tables a query should start from, first.
_TABLE_ORDER = ("observations", "hourlyforecasts", "obs_baro_impact", "loc_subset")


def _approx(n) -> str:
    """A table size as a reader wants it. Rounded on purpose: these are the planner's
    estimates, and an exact-looking count beside the scope line's reads as a
    discrepancy."""
    if not isinstance(n, int) or n < 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    return f"{n:,}"


def _first_sentence(text: str | None, limit: int = 170) -> str:
    text = " ".join((text or "").split())
    cut = re.search(r"(?<=[.!?])\s", text)
    text = text[: cut.start()] if cut else text
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _subjects_html(tables: list[dict]) -> str:
    rows = {t.get("table_name"): t.get("estimated_rows") for t in tables}
    tiles = []
    for table, title, fixed, text in _SUBJECTS:
        size = fixed or _approx(rows.get(table))
        tiles.append(
            '<div class="pp-subject"><p class="pp-subject-head">'
            f"<b>{html.escape(title)}</b>"
            + (f"<span>{html.escape(size)}</span>" if size else "")
            + f"</p><p>{html.escape(text)}</p></div>"
        )
    return f'<div class="pp-subjects">{"".join(tiles)}</div>'


def _tables_html(tables: list[dict]) -> str:
    rank = {name: i for i, name in enumerate(_TABLE_ORDER)}
    ordered = sorted(
        tables,
        key=lambda t: (rank.get(t.get("table_name"), 99), t.get("schema") != "public",
                       t.get("table_name") or ""),
    )
    return '<div class="pp-tables">' + "".join(
        f'<div class="pp-trow"><code>{html.escape(t.get("schema") or "")}.'
        f'{html.escape(t.get("table_name") or "")}</code>'
        f'<span class="pp-trows">{_approx(t.get("estimated_rows"))}</span>'
        f"<span>{_prose(_first_sentence(t.get('description')))}</span></div>"
        for t in ordered
    ) + "</div>"


# ONE CARD, NO TABS, as on Ask Football: what you can ask about at a glance; the
# caveats in plain words; how it works; and, collapsed, the tables for the reader
# who checks the SQL.
with st.container(key="card-reference"):
    st.html('<p class="pp-eyebrow">About the data</p>')
    try:
        tables = _schema()
    except Exception:  # noqa: BLE001 - the card is reference, not the page
        tables = []
    st.html(_subjects_html(tables))

    with st.expander("Good to know before you read a number"):
        st.html(
            '<ul class="pp-know">'
            + "".join(f"<li>{line}</li>" for line in GOOD_TO_KNOW)
            + "</ul>"
        )

    with st.expander("How answers are made"):
        st.markdown(
            f"""
**The chain.** Your question goes to `{agent_module.MODEL}`, which can reach data only
by calling three MCP tools: one lists the tables, one describes a table's columns and
units, and one runs SQL. That last one parses the query first, forces a `LIMIT`,
refuses anything but a single read-only `SELECT`, and runs it as a database role that
cannot write. This page holds no database password, so if it were compromised the
warehouse would be unaffected.

**Why it can still be wrong.** It writes the query; nothing checks that the query
answers *your* question. A model can write valid SQL that answers a slightly different
one, and the number looks the same either way. Read the SQL and the *Basis* line under
each answer. That is what they are there for.
"""
        )

    with st.expander("For SQL readers: the tables"):
        if tables:
            st.html(
                '<p class="pp-label">Tables · straight from the database</p>'
                + _tables_html(tables)
            )
        else:
            style.notice("The table list is unavailable right now.", "warn")


# The cross-site footer, last on the page -- see estate.py.
estate.footer(
    "Observations from NOAA's "
    '<a href="https://www.ncei.noaa.gov/products/global-historical-climatology-network-hourly" '
    f'target="_blank" rel="noopener noreferrer">GHCNh{estate.NEW_TAB_SR}</a>, forecasts '
    "from Apple WeatherKit. Answers are written by a language model and can be wrong "
    "even when the query runs; the SQL is shown under every answer so you can check it. "
    f"Each visit gets {budget.MAX_QUESTIONS_PER_SESSION} questions, and the page shares "
    "a daily model budget that resets at 00:00 UTC. "
    f'<a href="{REPO_URL}" target="_blank" rel="noopener noreferrer">'
    f"Source on GitHub{estate.NEW_TAB_SR}</a>.",
    model=agent_module.MODEL_FAMILY,
)
# Last of all: shows the page, which static/first-paint.css hides until the script is done.
style.reveal()

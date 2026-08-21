"""The "How it works" tab: text-to-SQL explained to someone who has not met it.

WHO THIS IS WRITTEN FOR
-----------------------
Someone who arrived from a portfolio link, understands "weather data" and
"question", and has never heard the phrase text-to-SQL. That reader does not
need the architecture first; they need to know that a database cannot answer a
question typed in English, that a language model's job here is translation, and
that translation is a thing that can go wrong in ways worth checking.

So the order is: what is happening → what could go wrong → how you check it →
what to type. The architecture is at the end, because it is the answer to a
question a newcomer has not asked yet.

NO JARGON WITHOUT A DEFINITION, AND NO CLAIM WITHOUT ITS COST
-------------------------------------------------------------
Every term of art on this page is defined the first time it appears: SQL, schema,
model, token, MCP. And the failure section is a real failure from this project's
own history, not a hypothetical -- a model reported Los Angeles as averaging
151 F in August because it converted a Fahrenheit column from Celsius. A page
that only advertises the upside teaches a visitor to trust the output, which is
precisely the wrong lesson to take away from a text-to-SQL demo.
"""

from __future__ import annotations

import streamlit as st

import ui

# The worked example, run against the live warehouse on 2026-08-21 -- not a
# mock-up. The SQL is reproduced as the server returned it: sqlglot re-prints
# every statement while parsing it for approval, which is why the formatting is
# not what a person would have typed, and why the keywords came back uppercased.
EXAMPLE_QUESTION = "Which states have the windiest weather on average?"

EXAMPLE_SQL = """\
SELECT
  r.state,
  ROUND(CAST(AVG(o.wnd) AS DECIMAL), 1) AS avg_wind_mph,
  COUNT(*) AS hours
FROM public.observations AS o
JOIN (
  SELECT DISTINCT
    station,
    state
  FROM public.loc_subset
) AS r
  ON r.station = o.station
WHERE
  o.wnd IS NOT NULL
GROUP BY
  r.state
ORDER BY
  avg_wind_mph DESC
LIMIT 5"""

EXAMPLE_ROWS = [
    ("HI", "12.0", "258,297"),
    ("WY", "12.0", "79,905"),
    ("KS", "11.0", "80,439"),
    ("MA", "10.9", "87,245"),
    ("ND", "10.9", "86,936"),
]


# --------------------------------------------------------------------------- #
# The pipeline diagram
# --------------------------------------------------------------------------- #

# Vertical, not horizontal. Five stages with a sentence of explanation each do
# not fit side by side at a legible size, and the labels on the arrows are the
# part that does the teaching -- a horizontal version has to drop them or shrink
# them to 9px. Vertical also survives a phone without scrolling sideways.
_STAGES = [
    (
        "You, in a browser",
        "type a question in ordinary English",
        "your question, as text",
    ),
    (
        "This web app",
        "adds a description of the available tables",
        "the question, plus what the tables contain",
    ),
    (
        "Gemini 3.7 Flash",
        "the language model — writes one SQL query",
        "a proposed SQL query",
    ),
    (
        "The query checker",
        "parses the SQL and approves or refuses it",
        "the approved query",
    ),
    (
        "PostgreSQL",
        "the database — 9.2M rows, opened read-only",
        None,
    ),
]


def pipeline_svg() -> str:
    """What happens between typing a question and seeing an answer.

    Takes no theme argument. Colours come from the CSS classes in ui.py, resolved
    in the browser, so this diagram tracks a Light/Dark switch without a rerun --
    see the module docstring there for why that matters.
    """
    box_w, box_h, step = 268, 62, 110
    left, rail = 60, 32
    top = 16
    height = top + step * (len(_STAGES) - 1) + box_h + 22

    out = [
        ui.svg_open(
            "sqlx-pipeline",
            620,
            height,
            "How a question becomes an answer",
            "Five stages in order: you type a question in a browser; the web app "
            "adds a description of the tables; Gemini 3.7 Flash writes one SQL "
            "query; a query checker parses and approves it; PostgreSQL runs it "
            "read-only. The rows, the chart and the query itself all travel back "
            "to the browser.",
            min_width=470,
        ),
        ui.svg_arrow_defs("pipe"),
        ui.svg_arrow_defs("pipe-back", accent=True),
    ]

    for index, (name, subtitle, edge) in enumerate(_STAGES):
        y = top + step * index
        # The model is the one stage a visitor has probably not seen in a
        # pipeline before, so it is the only one that gets the accent.
        is_model = name.startswith("Gemini")
        out.append(
            ui.svg_box(
                left, y, box_w, box_h, cls="sx-box-fact" if is_model else "sx-box"
            )
        )
        out.append(
            ui.svg_text(left + 16, y + 26, name, "sx-text sx-stage-title")
        )
        out.append(
            ui.svg_text(left + 16, y + 45, subtitle, "sx-text sx-stage-sub")
        )

        if edge:
            out.append(
                ui.svg_path(
                    f"M {left + 74} {y + box_h} V {y + step - 4}", marker="pipe"
                )
            )
            out.append(
                ui.svg_text(
                    left + box_w + 20, y + box_h + 30, edge, "sx-text sx-flow"
                )
            )

    # The return rail. Worth drawing because "you get the query back too" is the
    # whole premise of the page, and a one-way diagram quietly contradicts it.
    bottom = top + step * (len(_STAGES) - 1) + box_h - 14
    out.append(
        ui.svg_path(
            f"M {left} {bottom} H {rail} V {top + 30} H {left}",
            cls="sx-edge-key",
            marker="pipe-back",
        )
    )
    out.append(
        ui.svg_text(
            rail - 9,
            (top + bottom) // 2,
            "the rows, a chart, and the query that ran",
            "sx-text sx-rail",
            anchor="middle",
            rotate=-90,
        )
    )
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #


def render() -> None:
    """Draw the whole explainer. Reads nothing and costs nothing."""

    ui.prose(
        """
### A database cannot read English

The weather data behind this page lives in **PostgreSQL**, a database. Databases
do not answer questions phrased in English. They answer *queries* written in
**SQL** — a language you use to say precisely which rows to fetch, how to combine
tables, and how to summarise the result. Writing SQL well takes knowing the
language *and* knowing how this particular warehouse is laid out.

**Text-to-SQL** is the shortcut. A language model is given two things: your
question, and a description of the tables available. Its job is a translation —
English in, one SQL query out. The database then runs that query, exactly as it
would run a query a person had written, and the numbers that come back are the
database's own.

The model does not read the data to answer you. It never sees 9.2 million rows.
It writes a query, and Postgres does the arithmetic.
""",
        key="sqlx-prose-intro",
    )

    st.write("")
    # 620-unit canvas capped at 760px on screen. Uncapped, `width="100%"` scales
    # a narrow canvas up to fill the whole column and the labels come out
    # oversized; on a wider canvas the labels ended at 582 and left a quarter of
    # the figure blank. Narrow the canvas and cap the display size, both.
    ui.figure(
        pipeline_svg(),
        "Every question makes this round trip. The blue rail is what comes back "
        "to you — including the query itself, which is the part most tools of "
        "this kind keep hidden.",
        max_width=760,
    )

    st.divider()

    # ----------------------------------------------------------------- #
    # The worked example
    # ----------------------------------------------------------------- #
    ui.prose(
        """
### The same question, in all three languages

Here is one real round trip. This is not a mock-up — it is what the app returned
on 21 August 2026.
""",
        key="sqlx-prose-worked",
    )

    st.markdown("**1. What you type**")
    st.info(EXAMPLE_QUESTION, icon="💬")

    st.markdown("**2. The SQL the model wrote**")
    st.code(EXAMPLE_SQL, language="sql")
    st.caption(
        "Read it loosely and it says what you asked: average the wind column, "
        "group the rows by state, sort highest first, keep five. `wnd` is wind "
        "speed in miles per hour — the model looked that up rather than guessing, "
        "which is the step the **Data model** tab shows you."
    )

    st.markdown("**3. What the database sent back**")
    st.dataframe(
        {
            "state": [row[0] for row in EXAMPLE_ROWS],
            "avg_wind_mph": [row[1] for row in EXAMPLE_ROWS],
            "hours": [row[2] for row in EXAMPLE_ROWS],
        },
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Hawaii and Wyoming average 12.0 mph — the trade winds and the high "
        "plains, arriving at the same number for completely different reasons. "
        "The `hours` column is how many observations each average rests on, which "
        "is the first thing to check before believing any average."
    )

    st.divider()

    # ----------------------------------------------------------------- #
    # Why the query is on the page
    # ----------------------------------------------------------------- #
    ui.prose(
        """
### Why the query is shown, and not tucked away

A language model that writes a wrong query does not announce it. It returns a
number, in a confident sentence, in the same format as a right answer. There is
no error message, because nothing errored — the SQL was valid, it just did not
mean what you asked.

That is not a theoretical risk. It happened while this app was being built:
""",
        key="sqlx-prose-trust",
    )

    ui.callout(
        "A real wrong answer, from this project",
        "Asked to compare August forecasts against history, the model wrote a "
        "query that converted the <code>tmp</code> column from Celsius to "
        "Fahrenheit. Reasonable-looking — except <code>tmp</code> is "
        "<b>already Fahrenheit</b>. The query ran fine and reported that Los "
        "Angeles averages <b>151&#8202;°F</b> in August.",
        "Nothing in the output flagged it. The sentence was fluent, the query was "
        "valid SQL, and the number was nonsense. The only way to catch it was to "
        "read the query.",
        "The fix was not a better prompt. The units now live in the database "
        "itself, as comments on every column, and the model is required to read "
        "them before writing SQL. You can read exactly what it reads on the "
        "<b>Data model</b> tab.",
    )

    ui.prose(
        """
So every answer on this page arrives with its query attached, and the full result
table underneath the chart. Three things worth a glance before you trust a number:

- **Which columns did it use?** `tmp` is temperature, `wnd` is wind, `slp` is
  pressure. Picking the wrong one produces a perfectly valid wrong answer.
- **Did it filter the way you meant?** A `WHERE` clause that narrows to the wrong
  dates or the wrong stations is the most common quiet mistake.
- **How many rows is the answer built on?** An average over 40 hours and an
  average over 40,000 are both just a number on screen.

None of this requires you to write SQL. It only requires that the query be
visible, which is the thing this app refuses to hide.
""",
        key="sqlx-prose-checks",
    )

    st.divider()

    # ----------------------------------------------------------------- #
    # Asking well
    # ----------------------------------------------------------------- #
    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("##### Questions that work well")
        st.markdown(
            """
- **Name a place, a state, or a region.** "Denver", "Texas", "the Mountain
  West" all resolve; the warehouse covers 112 US stations.
- **Name a measure.** Temperature, dew point, wind, pressure, visibility,
  precipitation, cloud ceiling.
- **Ask for a summary, a ranking, or a trend** — "the 10 hottest hours", "average
  by month", "which stations changed most". Aggregates are what a warehouse is
  for.
- **Bound the time range.** "since 2019", "last 30 days", "every August".
- **Compare forecast against history.** Both are in the same database, so
  "forecast versus the station's own normal" is one query.
"""
        )

    with right:
        st.markdown("##### Questions that will not work")
        st.markdown(
            """
- **Anywhere outside the US**, or a town without a weather station. The roster is
  112 stations, all US, and the **Data model** tab lists them.
- **Before 2019**, or more than about two weeks ahead. That is the whole span
  held.
- **Measures that were never recorded** — snow depth, UV index, air quality,
  lightning. Not in these tables.
- **Why questions.** "Why was last winter mild" is not a query; the data can
  describe what happened, not explain it.
- **Anything that changes data.** The database connection holds read permission
  and nothing else, so writes cannot succeed even if asked for.
"""
        )

    st.divider()

    # ----------------------------------------------------------------- #
    # The architecture, last, for the reader who wants it
    # ----------------------------------------------------------------- #
    with st.expander("Under the hood — the parts, and why the model is kept away from the database"):
        ui.prose(
            """
The model writes SQL, but it never holds a database password. Nothing in the
public-facing web app does.

Between them sits a small server speaking **MCP** (Model Context Protocol, a
standard way to hand a language model a fixed set of tools). It offers exactly
three:

| Tool | What the model can do with it |
|---|---|
| `list_schema` | see which tables exist |
| `describe_table` | read one table's columns, types and **units** |
| `run_sql` | run **one** read-only `SELECT` |

There is no fourth tool, so there is no way to ask for anything else. Every
statement is parsed into a syntax tree before it runs and refused unless its
*shape* is a single read-only query over one of ten approved tables — not by
searching the text for dangerous words, which is a game you lose, but by checking
what the statement actually is. Underneath that, the database role itself holds
`SELECT` and nothing more, every transaction is read-only, and queries are killed
at 30 seconds.

Three independent layers, and the innermost one is the database's own permission
system. A query that got past the first two still cannot write, because the
account it runs as has never been able to.

**On cost.** Each question spends real tokens with the model provider — a *token*
is roughly a word-fragment, and it is billed. This page is public with no login,
so there are hard caps: a limit per browser session, a global daily ceiling, and
a limit on how many steps one question may take. When you run out, questions
pause rather than quietly costing more. Reading the schema on the **Data model**
tab spends nothing: no model is involved in that.
""",
            key="sqlx-prose-arch",
        )

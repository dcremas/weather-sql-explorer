"""The "Data model" tab: what tables exist, what the columns mean, how they join.

WHY THIS PAGE READS THE DATABASE INSTEAD OF DESCRIBING IT
---------------------------------------------------------
Every column note on this page is a `COMMENT` fetched out of pg_catalog at
render time, through the same two MCP tools the model itself uses -- so the
documentation a visitor reads and the documentation the model reads are the same
bytes. There is no second copy to fall out of step, and no way for this page to
claim `tmp` is Fahrenheit after someone has changed the database to say
otherwise.

That is also the honest answer to "how does the model know what `slp` means?".
It does not know; it looks. This page is that lookup, shown to a person.

WHAT IS STATIC HERE, AND WHY
----------------------------
Two things are hardcoded, because the database does not hold them:

  * `_LAYOUT` -- where each box sits in the entity map. Positions are a design
    decision, not data.
  * `_EDGES` -- which column joins to which. These are conventions, not declared
    foreign keys: `observations.station` has no FK to `loc_subset`, and adding
    one is not this app's call to make. So the relationships are written down
    here, where a reader can see them, rather than inferred from a constraint
    that does not exist.

Row counts, column lists and descriptions are all live. If the fetch fails, the
map still draws -- see `render` -- because a diagram of the tables is still worth
having when the server is down.

NOTHING ON THIS PAGE SPENDS A TOKEN
-----------------------------------
`agent.call_tool` reaches MCP directly with no model in the path, so browsing the
schema is free and is not charged to the budget ledger. That matters: a visitor
should be able to work out what to ask BEFORE spending one of their twelve
questions finding out the data does not go back to 2015.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

import agent as agent_module
import ui

# Every table run_sql may read. Ordered as the page presents them: the two fact
# tables first, then what they join to.
TABLES = (
    "public.observations",
    "public.loc_subset",
    "public.locations",
    "public.regions",
    "public.obs_baro_impact",
    "awk.hourlyforecasts",
    "awk.hf_baro_impact",
    "awk.locations",
    "awk.regions",
    "awk.time_zones",
)

# Postgres type names are precise and unfriendly. A visitor deciding whether they
# can ask about a column needs "number" or "text", not "double precision".
_FRIENDLY_TYPES = {
    "character varying": "text",
    "text": "text",
    "double precision": "number",
    "numeric": "number",
    "integer": "whole number",
    "bigint": "whole number",
    "smallint": "whole number",
    "boolean": "true/false",
    "date": "date",
    "timestamp without time zone": "date + time",
    "timestamp with time zone": "date + time (with zone)",
}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def _payload(raw: Any) -> dict:
    """Decode one MCP tool result into a dict.

    langchain-mcp-adapters hands back the tool's text content, which for this
    server is always a JSON object -- but it hands it back as a bare string, a
    list of typed content blocks, or a (content, artifact) tuple depending on
    version and response format. All three shapes are unwrapped here rather than
    pinning a version, because the one that breaks is whichever one is not
    handled.
    """
    if isinstance(raw, tuple) and raw:
        raw = raw[0]

    text = ""
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, dict):
        return raw
    elif isinstance(raw, list):
        for block in raw:
            if isinstance(block, str):
                text += block
            elif isinstance(block, dict):
                text += block.get("text", "")

    try:
        decoded = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Unreadable tool result: {text[:200]!r}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"Expected an object from the tool, got {type(decoded)}")
    return decoded


# Cached across sessions, not just per session: this is the same answer for every
# visitor, and it costs eleven round trips plus a scan of the fact table. The TTL
# exists because the warehouse is loaded daily -- an hour-old row count is fine,
# a row count from last week's process is not.
@st.cache_data(ttl=3600, show_spinner=False)
def fetch() -> dict:
    """The live schema: every table, every column, plus real counts and coverage.

    Raises whatever `call_tool` raises. The caller is expected to catch it and
    fall back -- see `render`.
    """
    schema = _payload(agent_module.call_tool("list_schema"))

    tables: dict[str, dict] = {}
    for name in TABLES:
        described = _payload(agent_module.call_tool("describe_table", {"table": name}))
        tables[name] = {
            "meta": described.get("table") or {},
            "columns": described.get("columns") or [],
        }

    # Real numbers for the things the planner cannot tell us. `estimated_rows`
    # reads -1 for every foreign table because the local planner keeps no
    # statistics for them -- which means "unknown", and showing it as a row count
    # would read as "empty". So those get counted for real.
    #
    # WHAT IS NOT IN THIS QUERY, AND WHY. `count(DISTINCT station)` over the
    # 9.2M-row fact table measured 5.4s on a cold buffer cache -- five seconds
    # added to the first page load after every restart, for a number that is
    # also, exactly, the length of the station roster fetched below. min/max on
    # `date` is indexed and effectively free; the plain `count(*)` is under a
    # second. Both stay.
    counts = _payload(
        agent_module.call_tool(
            "run_sql",
            {
                "sql": (
                    "SELECT (SELECT count(*) FROM awk.hourlyforecasts) AS forecast_rows, "
                    "(SELECT count(*) FROM awk.hf_baro_impact) AS forecast_baro_rows, "
                    "min(date)::date AS first_obs, max(date)::date AS last_obs, "
                    "count(*) AS observation_rows "
                    "FROM public.observations"
                )
            },
            timeout=60.0,
        )
    )
    facts = dict(zip(counts.get("columns") or [], (counts.get("rows") or [[]])[0]))

    roster = _payload(
        agent_module.call_tool(
            "run_sql",
            {
                "sql": (
                    "SELECT DISTINCT station, station_name, state, region, sub_region, "
                    "lat, lon FROM public.loc_subset ORDER BY state, station_name"
                )
            },
            timeout=60.0,
        )
    )

    roster_rows = roster.get("rows") or []
    # The roster is one row per station, so its length IS the station count --
    # see the note on the facts query above.
    facts["stations"] = len(roster_rows)

    return {
        "sql_rules": schema.get("sql_rules", ""),
        "tables": tables,
        "facts": facts,
        "roster": {
            "columns": roster.get("columns") or [],
            "rows": roster_rows,
        },
    }


def _row_count(name: str, live: dict | None) -> int | None:
    """The number of rows in one table, or None when it is genuinely unknown."""
    if not live:
        return None
    overrides = {
        "awk.hourlyforecasts": "forecast_rows",
        "awk.hf_baro_impact": "forecast_baro_rows",
        "public.observations": "observation_rows",
    }
    if name in overrides:
        value = live["facts"].get(overrides[name])
        return int(value) if value is not None else None
    estimate = (live["tables"].get(name, {}).get("meta") or {}).get("estimated_rows")
    if estimate is None or int(estimate) < 0:
        return None
    return int(estimate)


# --------------------------------------------------------------------------- #
# The entity map
# --------------------------------------------------------------------------- #

# GEOMETRY. x, y, width, height, then the label and the two lines of column text.
#
# EVERY NUMBER HERE IS LOAD-BEARING AND THE BOX HEIGHTS ARE THE EASIEST TO GET
# WRONG. A small box draws four lines of text at +23, +40, +56 and +70 from its
# top, so its height must exceed 70 plus a descender. The first version of this
# used h=68 with the same offsets, and the second column line rendered *below*
# the box border on all five small tables -- which looks like a text-overflow
# bug and is really an arithmetic one. If you change an offset, change the
# height, and check the two-line boxes specifically.
#
# Column text is monospace at 9.5 units. Usable width is the box width less 21
# units of padding, so roughly (w - 21) / 5.7 characters: 26 for a 170-wide box,
# 33 for a 214. Keep the strings under that -- there is no wrapping in SVG.
#
# The left column is 170 wide rather than 190 to buy gap, not because the boxes
# needed to be smaller. An edge label is centred in the space between two boxes,
# and "station" at 10 units is about 42 wide: with 190-wide boxes the gap to
# `observations` was 38, so the label overhung both sides and printed across the
# fact table's border. Narrowing the column to 170 opens the gap to 54.
#
# Heights differ between tables on purpose: the two fact tables are taller
# because they are the tables a question actually lands on, and the map should
# say so without needing a key.
_LAYOUT: dict[str, dict] = {
    "public.observations": {
        "box": (252, 170, 214, 92),
        "label": "observations",
        "lines": ["station · date", "tmp dew slp wnd vis prp cig"],
        "fact": True,
    },
    "public.loc_subset": {
        "box": (24, 186, 170, 78),
        "label": "loc_subset",
        "lines": ["station · station_name", "state · region · year"],
    },
    "public.locations": {
        "box": (24, 296, 170, 78),
        "label": "locations",
        "lines": ["station · lat · lon", "elevation · begin · end"],
    },
    "public.regions": {
        "box": (24, 76, 170, 78),
        "label": "regions",
        "lines": ["state", "region · sub_region"],
    },
    "public.obs_baro_impact": {
        "box": (252, 320, 214, 78),
        "label": "obs_baro_impact",
        "lines": ["station · date", "slp_3hr / 6hr / 24hr_diff"],
    },
    "awk.hourlyforecasts": {
        "box": (568, 170, 214, 92),
        "label": "hourlyforecasts",
        "lines": ["station · time", "temp_f wind_mph pressure_in …"],
        "fact": True,
        "foreign": True,
    },
    "awk.hf_baro_impact": {
        "box": (568, 320, 214, 78),
        "label": "hf_baro_impact",
        "lines": ["station · time", "pressure deltas, looking ahead"],
        "foreign": True,
    },
}

# Reference tables drawn as one grouped box rather than three. list_schema calls
# awk.locations and awk.regions DUPLICATE and says to prefer the public copies,
# so giving each its own node would spend the map's most valuable space
# advertising tables nobody should reach for first. For the same reason no edge
# is drawn into this group: the join exists, but pointing at it would suggest
# it is a route worth taking.
_REFERENCE_GROUP = (812, 170, 170, 228)
_REFERENCE_TABLES = ("awk.locations", "awk.regions", "awk.time_zones")

# Join conventions. Not foreign keys -- see the module docstring.
# (path, label, label x, label y, anchor)
_EDGES = [
    ("M 109 154 V 182", "state", 117, 173, "start"),
    ("M 194 225 H 248", "station", 221, 218, "middle"),
    ("M 194 335 H 222 V 245 H 248", "station", 228, 300, "start"),
    ("M 359 262 V 316", "station", 367, 293, "start"),
    ("M 675 262 V 316", "station", 683, 293, "start"),
]


def entity_svg(live: dict | None) -> str:
    """The map of the ten readable tables and how they connect.

    Takes no theme argument. Colours come from the CSS classes in ui.py, resolved
    in the browser, so this map tracks a Light/Dark switch without a rerun -- see
    the module docstring there for why that matters.
    """
    width, height = 1000, 474

    out = [
        ui.svg_open(
            "sqlx-entities",
            width,
            height,
            "The ten readable tables and how they join",
            "Two groups of tables. On the left, the local historical schema: "
            "observations is the main fact table, joined on station to "
            "loc_subset, locations and obs_baro_impact, with regions joined to "
            "loc_subset on state. On the right, reached over postgres_fdw, the "
            "Apple WeatherKit forecast schema: hourlyforecasts and "
            "hf_baro_impact, plus three reference copies of the station and "
            "region tables. observations joins hourlyforecasts on station, which "
            "is what lets one query compare a forecast against that station's "
            "own history.",
            min_width=880,
        ),
        ui.svg_arrow_defs("ent"),
        ui.svg_arrow_defs("ent-key", accent=True),
    ]

    # Panels ---------------------------------------------------------------
    out.append(ui.svg_box(8, 40, 484, 380, cls="sx-panel", radius=10))
    out.append(ui.svg_box(548, 40, 444, 380, cls="sx-panel", radius=10))
    out.append(
        ui.svg_text(24, 26, "public — NOAA GHCNh history", "sx-text sx-head")
    )
    out.append(
        ui.svg_text(24, 62, "local tables, in this database", "sx-text sx-sub")
    )
    out.append(
        ui.svg_text(568, 26, "awk — Apple WeatherKit forecast", "sx-text sx-head")
    )
    out.append(
        ui.svg_text(
            568, 62, "foreign tables, in a second database", "sx-text sx-sub"
        )
    )

    # The FDW seam ---------------------------------------------------------
    out.append(ui.svg_path("M 520 40 V 420", cls="sx-seam"))
    # Low on the seam, not centred on it. Centred, this rotated label sat exactly
    # where the `station` join label crosses, and the two overprinted.
    out.append(
        ui.svg_text(
            514,
            350,
            "postgres_fdw",
            "sx-text sx-seam-label",
            anchor="middle",
            rotate=-90,
        )
    )

    # Edges before boxes, so a line never draws over a label ---------------
    for path, label, lx, ly, anchor in _EDGES:
        out.append(ui.svg_path(path, cls="sx-edge", marker="ent"))
        out.append(
            ui.svg_text(lx, ly, label, "sx-text sx-mono sx-label", anchor=anchor)
        )

    # The one edge the whole warehouse is arranged around. Two arrow heads,
    # because a join is not a direction.
    #
    # ONE LABEL, NOT TWO. An earlier version also wrote "forecast vs. history"
    # here. At 10.5 units that string is ~100 wide and the gap between the two
    # fact boxes is 98, so it ran under both of them and under the seam label as
    # well. The line's weight and its two arrow heads already mark it as the
    # important one, and the caption under the figure says what it is for.
    out.append(ui.svg_join("M 466 210 H 564", "ent-key"))
    out.append(
        ui.svg_text(
            515, 200, "station", "sx-text sx-mono sx-label-key", anchor="middle"
        )
    )

    # Boxes ----------------------------------------------------------------
    for name, spec in _LAYOUT.items():
        x, y, w, h = spec["box"]
        is_fact = spec.get("fact", False)
        classes = "sx-box-fact" if is_fact else "sx-box"
        if spec.get("foreign", False):
            classes += " sx-foreign"
        out.append(ui.svg_box(x, y, w, h, cls=classes))
        out.append(
            ui.svg_text(
                x + 13,
                y + 24,
                spec["label"],
                "sx-text sx-mono " + ("sx-title-fact" if is_fact else "sx-title"),
            )
        )

        count = _row_count(name, live)
        columns = len((live or {}).get("tables", {}).get(name, {}).get("columns") or [])
        summary = f"{count:,} rows" if count is not None else "row count unknown"
        if columns:
            summary += f" · {columns} columns"
        out.append(ui.svg_text(x + 13, y + 41, summary, "sx-text sx-meta"))

        # +56 and +70 from the box top. Both must stay inside the height set in
        # _LAYOUT -- see the warning there.
        for offset, line in enumerate(spec["lines"]):
            out.append(
                ui.svg_text(
                    x + 13, y + 56 + offset * 14, line, "sx-text sx-mono sx-cols"
                )
            )

    # The reference group --------------------------------------------------
    gx, gy, gw, gh = _REFERENCE_GROUP
    out.append(ui.svg_box(gx, gy, gw, gh, cls="sx-box sx-foreign"))
    out.append(
        ui.svg_text(gx + 13, gy + 24, "reference copies", "sx-text sx-title")
    )
    for offset, name in enumerate(_REFERENCE_TABLES):
        out.append(
            ui.svg_text(
                gx + 13, gy + 50 + offset * 19, name, "sx-text sx-mono sx-cols"
            )
        )
    for offset, line in enumerate(
        (
            "duplicates of the",
            "public.* tables — use",
            "those instead, unless",
            "you need time zones",
        )
    ):
        out.append(
            ui.svg_text(
                gx + 13, gy + 150 + offset * 14, line, "sx-text sx-note"
            )
        )

    # Legend ---------------------------------------------------------------
    out.append(ui.svg_box(24, 440, 22, 13, cls="sx-box", radius=3))
    out.append(
        ui.svg_text(54, 451, "a table in this database", "sx-text sx-legend")
    )
    out.append(ui.svg_box(224, 440, 22, 13, cls="sx-box sx-foreign", radius=3))
    out.append(
        ui.svg_text(
            254,
            451,
            "a table in the other database, readable from here",
            "sx-text sx-legend",
        )
    )
    out.append(ui.svg_box(596, 440, 22, 13, cls="sx-box-fact", radius=3))
    out.append(
        ui.svg_text(626, 451, "where the measurements are", "sx-text sx-legend")
    )
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #

_JOIN_RECIPES = [
    (
        "Give the station codes human names",
        "`station` is an opaque 11-character code. Names live in `loc_subset`, "
        "which holds one row per station per *year* — so `DISTINCT` is not "
        "optional here. Without it you multiply your row count by the number of "
        "years.",
        """SELECT s.station_name, s.state, round(avg(o.tmp)::numeric, 1) AS avg_f
FROM public.observations o
JOIN (SELECT DISTINCT station, station_name, state
      FROM public.loc_subset) s USING (station)
GROUP BY s.station_name, s.state
ORDER BY avg_f DESC""",
    ),
    (
        "Compare the forecast against a station's own history",
        "The join that the whole two-schema arrangement exists for. **Aggregate "
        "each side first, then join the summaries.** Joining the raw tables on "
        "`station` pairs 9.2M observation rows with ~480 forecast rows each — "
        "about 4.4 billion intermediate rows, and it hits the 30-second timeout "
        "every time.",
        """WITH fc AS (
  SELECT station, avg(temp_f) AS forecast_f
  FROM awk.hourlyforecasts GROUP BY station
), hist AS (
  SELECT station, avg(tmp) AS normal_f
  FROM public.observations
  WHERE extract(month FROM date) = 8
  GROUP BY station
)
SELECT station, forecast_f, normal_f, forecast_f - normal_f AS anomaly_f
FROM hist JOIN fc USING (station)
ORDER BY anomaly_f DESC""",
    ),
    (
        "Roll stations up to a region",
        "Two routes. `loc_subset` already carries `region` and `sub_region`, "
        "which is the short way. `regions` joins on `state` and is there for when "
        "you start from a state list instead.",
        """SELECT s.region, round(avg(o.wnd)::numeric, 1) AS avg_wind_mph
FROM public.observations o
JOIN (SELECT DISTINCT station, region FROM public.loc_subset) s
  USING (station)
WHERE o.wnd IS NOT NULL
GROUP BY s.region
ORDER BY avg_wind_mph DESC""",
    ),
]


def _friendly(data_type: str) -> str:
    return _FRIENDLY_TYPES.get(data_type, data_type)


def _table_card(name: str, entry: dict, count: int | None) -> None:
    """One table's columns, with whatever the database says each one means."""
    meta = entry.get("meta") or {}
    columns = entry.get("columns") or []

    facts = [f"**{len(columns)}** columns"]
    facts.append(f"**{count:,}** rows" if count is not None else "row count not tracked")
    if meta.get("kind") == "foreign table":
        facts.append("foreign table")
    st.caption(" &nbsp;·&nbsp; ".join(facts))

    description = meta.get("description")
    if description:
        st.markdown(description)
        st.caption(
            "That is the table's own description, as stored in the database and "
            "as handed to the model — written to stop it writing a bad query, "
            "not to read nicely."
        )
    else:
        st.caption(
            "No description recorded in the database for this table. "
            "The model sees the same nothing."
        )

    # The units live in the "what it holds" column, so that is the column that
    # must not be clipped -- an elided "Air temperature in DEGREES FAHRENHE…" is
    # the one detail on this page a visitor actually came for. Hence `width`
    # here, and hence these cards being full width rather than in two columns.
    st.dataframe(
        {
            "column": [c.get("column_name", "") for c in columns],
            "type": [_friendly(c.get("data_type", "")) for c in columns],
            "what it holds": [c.get("description") or "—" for c in columns],
        },
        hide_index=True,
        width="stretch",
        key=f"schema-{name}",
        column_config={
            "column": st.column_config.TextColumn(width="small"),
            "type": st.column_config.TextColumn(width="small"),
            "what it holds": st.column_config.TextColumn(width="large"),
        },
    )


def render() -> None:
    """Draw the tab. Degrades to the map alone if the server is unreachable."""

    live: dict | None = None
    problem: str | None = None
    try:
        live = fetch()
    except Exception as exc:  # noqa: BLE001 - becomes an on-page message
        problem = str(exc)

    ui.prose(
        """
### What is actually in here

Two databases, presented as one. Ten tables are readable, and every question you
ask is answered from these and nothing else — so this page is the honest limit of
what the app can tell you.

Everything below is read out of the database when this page loads: the row
counts, the column lists, and the description of each column. It is the same
lookup the model does before it writes a query, which is why the two cannot
disagree. **Reading it costs nothing** — no model is involved, and it does not
count against your questions.
""",
        key="sqlx-prose-datamodel",
    )

    if problem:
        st.warning(
            "The live schema could not be read, so the row counts and column "
            f"lists below are missing. The map still applies. ({problem})"
        )

    facts = (live or {}).get("facts", {})
    if facts:
        # Four separate metrics rather than one date-range metric: st.metric
        # truncates a long value with an ellipsis, and "2019-01-01 →
        # 2026-08-19" lost its second half -- which is the half that says how
        # current the data is.
        chips = st.columns(4, gap="small")
        with chips[0]:
            st.metric("Hourly observations", f"{int(facts.get('observation_rows', 0)):,}")
        with chips[1]:
            st.metric("Weather stations", f"{int(facts.get('stations', 0))}")
        with chips[2]:
            st.metric("Most recent reading", str(facts.get("last_obs", "?")))
        with chips[3]:
            st.metric("Forecast rows", f"{int(facts.get('forecast_rows', 0)):,}")
        st.caption(
            f"Counted from the tables just now, not from a note someone wrote. "
            f"History runs from **{facts.get('first_obs', '?')}** to the date "
            "above. The forecast is a rolling window roughly two weeks ahead, so "
            "its size stays about the same while its contents move."
        )

    st.write("")
    ui.figure(
        entity_svg(live),
        "The two blue tables hold the measurements; everything else exists to "
        "tell you where and when. Almost every join in this warehouse is on "
        "<code>station</code> — including the one across the dashed line, which "
        "is how a forecast gets compared against the same station's own record.",
    )

    st.divider()

    # ------------------------------------------------------------------ #
    # Column dictionary
    # ------------------------------------------------------------------ #
    st.markdown("### The columns, table by table")
    st.caption(
        "Abbreviated names with non-obvious units are the main way a query goes "
        "quietly wrong here — `tmp` is already Fahrenheit and `slp` is inches of "
        "mercury, neither of which you would guess. Start with `observations`."
    )

    # Stacked full width, not side by side. Two columns halves the space
    # available to the description column, which is where the units are, and the
    # units are the whole reason to read this. Only the two fact tables open by
    # default, so the section is not as long as ten expanders sounds.
    if live:
        st.markdown("**`public` — the historical archive**")
        for name in TABLES[:5]:
            with st.expander(name, expanded=(name == "public.observations")):
                _table_card(name, live["tables"].get(name, {}), _row_count(name, live))

        st.write("")
        st.markdown("**`awk` — the forecast, reached over postgres_fdw**")
        for name in TABLES[5:]:
            with st.expander(name, expanded=(name == "awk.hourlyforecasts")):
                _table_card(name, live["tables"].get(name, {}), _row_count(name, live))

    st.divider()

    # ------------------------------------------------------------------ #
    # Joins
    # ------------------------------------------------------------------ #
    st.markdown("### How the tables get combined")
    st.caption(
        "You never have to write any of this — it is here so the queries on the "
        "**Ask** tab are recognisable when they appear, and because two of these "
        "three shapes are the ones a query gets wrong."
    )
    for heading, note, sql in _JOIN_RECIPES:
        with st.container(border=True):
            st.markdown(f"**{heading}**")
            st.markdown(note)
            st.code(sql, language="sql")

    st.divider()

    # ------------------------------------------------------------------ #
    # The station roster: the answer to "can I ask about my town?"
    # ------------------------------------------------------------------ #
    st.markdown("### Every station you can ask about")
    roster = (live or {}).get("roster") or {}
    rows = roster.get("rows") or []
    if rows:
        st.caption(
            f"All {len(rows)} of them, straight from `loc_subset`. Airports, "
            "mostly — that is where hourly surface observations come from. Sort "
            "or search this table to check a place before you spend a question "
            "on it; if a town is not here, no station near it reported."
        )
        st.dataframe(
            {
                column: [row[index] for row in rows]
                for index, column in enumerate(roster.get("columns") or [])
            },
            hide_index=True,
            width="stretch",
            height=420,
            key="schema-roster",
        )
    else:
        st.caption("The station roster could not be read.")

    rules = (live or {}).get("sql_rules")
    if rules:
        with st.expander("The rules every generated query has to satisfy"):
            st.caption(
                "Reproduced exactly as the server states them to the model. A "
                "query breaking any of these is refused before it reaches the "
                "database."
            )
            st.code(rules, language="text")

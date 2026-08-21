"""Deciding whether an arbitrary result set should be charted, and drawing it.

THE HARD PART IS RESTRAINT
--------------------------
Every other chart in this repo plots a known query. Here the shape is whatever the
model wrote, so the module has to answer "should this be a chart at all?" before
"which chart?". Guessing wrong is worse than not charting: a bar chart of 400
station ids, or of a column that happens to be a year, is actively misleading, and
a visitor cannot tell a bad auto-chart from a bad answer.

So the default is NO CHART, and a result has to earn one:

  * 2 to 40 rows. One row is a number, not a chart. Past 40 the labels collide
    and the table is easier to read anyway.
  * exactly one label-ish column (text, or a date/time), which becomes the axis.
    Two text columns is usually a lookup listing, where neither is "the" axis.
  * at least one numeric column that is not an id.

WHAT IS DELIBERATELY NOT DONE
-----------------------------
No dual axis, ever -- it is the single most common charting mistake, and with
model-chosen columns it would be trivially easy to plot inHg against Fahrenheit on
two scales and call it a comparison. Instead, several measures are only drawn
together when their magnitudes are actually comparable; otherwise the extra
measures are dropped from the chart (never silently -- the caller is told, and the
full table is always on the page).
"""

from __future__ import annotations

import re

import pandas as pd
import plotly.graph_objects as go

import theme

MIN_ROWS = 2
MAX_ROWS = 40

# Two measures may share an axis only if their largest absolute values are within
# this factor. 10x is generous enough for "forecast vs 30-year normal" (the query
# this app is built to show off, both in F, near-identical magnitude) and tight
# enough to reject temperature plotted against a row count.
COMPARABLE_MAGNITUDE_RATIO = 10.0

# Columns that are identifiers rather than measures. `station` is the obvious one:
# it is an 11-digit code, so it is numeric-looking and averaging or plotting it is
# meaningless. Matched on the name because the type cannot tell them apart.
_ID_PATTERN = re.compile(
    r"(^|_)(id|ids|station|usaf|wban|icao|code|zip|fips)($|_)", re.IGNORECASE
)

# Numeric columns that are really a time axis. `EXTRACT(YEAR FROM date)` returns a
# NUMBER, so "average August temperature per year" arrives as two numeric columns
# and nothing text-shaped -- which the label/measure split rejects as having no
# axis at all. That is one of the most natural questions to ask of this warehouse,
# so a numeric period column is promoted to the axis rather than declined.
#
# Matched on the name, because 2019 and 76.2 are both just floats.
_PERIOD_PATTERN = re.compile(
    r"(^|_)(year|yr|month|mon|day|hour|hr|week|quarter|decade|period|bucket)($|_)",
    re.IGNORECASE,
)


def _is_identifier(name: str) -> bool:
    return bool(_ID_PATTERN.search(name or ""))


# A fallback for when the name gives nothing away. The model is asked to alias
# time buckets clearly (see agent.py's system prompt), but it does not always --
# `EXTRACT(YEAR FROM date) AS y` is a real observed case, and `y` matches no
# pattern. Values in this range that are whole numbers are years in practice: no
# temperature, pressure, wind speed or visibility in this warehouse lands here.
_YEAR_MIN, _YEAR_MAX = 1850, 2200


def _is_period(name: str) -> bool:
    return bool(_PERIOD_PATTERN.search(name or ""))


def _looks_like_years(series: pd.Series) -> bool:
    """Whether a numeric column is plausibly a year, judged on its values."""
    values = series.dropna()
    if len(values) < 2 or values.nunique() < 2:
        return False
    if not ((values >= _YEAR_MIN) & (values <= _YEAR_MAX)).all():
        return False
    # Whole numbers only: 2019.0 is a year, 2019.4 is a measurement.
    return bool((values % 1 == 0).all())


def build_frame(columns: list[str], rows: list[list]) -> pd.DataFrame:
    """Assemble a DataFrame, recovering the types JSON erased.

    run_sql serialises timestamps to ISO strings and Decimals to floats, so
    everything text-shaped arrives as `object`. Without re-parsing, a time series
    would be treated as a categorical axis and sorted lexically -- which mostly
    looks right and is wrong at the boundaries ('2026-1-2' after '2026-10-1').
    """
    frame = pd.DataFrame(rows, columns=columns)

    for name in frame.columns:
        # `dtype != object` is NOT the right test here, and getting it wrong is
        # silent. pandas 3.0 gives text columns a dedicated `str` dtype rather
        # than `object`, so an object-only check skips every string column: dates
        # stayed strings, `temporal` came back empty, and a time series was drawn
        # as a bar chart with a lexically sorted axis. It looked plausible, which
        # is why it needs a comment rather than just a fix.
        if not (
            pd.api.types.is_object_dtype(frame[name])
            or pd.api.types.is_string_dtype(frame[name])
        ):
            continue
        parsed = pd.to_datetime(frame[name], errors="coerce", format="ISO8601")
        # Only accept the conversion if essentially the whole column parsed;
        # a column with one date-looking value among names is not a date column.
        if parsed.notna().mean() > 0.9:
            frame[name] = parsed

    return frame


def _classify(frame: pd.DataFrame) -> tuple[list[str], list[str], list[str], list[str]]:
    """Split columns into (temporal, periodic, categorical, numeric-measure).

    `periodic` is the numeric-but-really-a-time-axis case -- see _PERIOD_PATTERN.
    It is kept separate from both `temporal` and `numeric` because it is only an
    axis when there is nothing better, and it must never be treated as a measure.
    """
    temporal, periodic, categorical, numeric = [], [], [], []
    for name in frame.columns:
        series = frame[name]
        if pd.api.types.is_datetime64_any_dtype(series):
            temporal.append(name)
        elif pd.api.types.is_numeric_dtype(series) and not _is_identifier(name):
            if _is_period(name) or _looks_like_years(series):
                periodic.append(name)
            else:
                numeric.append(name)
        else:
            categorical.append(name)
    return temporal, periodic, categorical, numeric


def plan(frame: pd.DataFrame) -> dict:
    """Decide whether and how to chart this frame.

    Returns {"chartable": False, "reason": ...} or a plan naming the form, the
    axis column, and which measures may share the axis.
    """
    if frame.empty:
        return {"chartable": False, "reason": "The query returned no rows."}
    if len(frame) < MIN_ROWS:
        return {
            "chartable": False,
            "reason": "A single row is a number, not a chart — see the table below.",
        }
    if len(frame) > MAX_ROWS:
        return {
            "chartable": False,
            "reason": (
                f"{len(frame):,} rows is too many to chart legibly "
                f"(limit {MAX_ROWS}). Ask for a top-N or an aggregate to see a chart."
            ),
        }

    temporal, periodic, categorical, numeric = _classify(frame)

    if not numeric:
        return {
            "chartable": False,
            "reason": "No measure column to plot — every column is a label or an id.",
        }

    axis = _choose_axis(frame, temporal, periodic, categorical)
    if axis is None:
        return {
            "chartable": False,
            "reason": (
                "No column works as an axis — the table below is the clearer view."
            ),
        }

    # A sequence gets a line; a set of named things gets bars.
    form = "line" if axis in temporal or axis in periodic else "bar"

    measures, dropped = _shared_axis_measures(frame, numeric)

    return {
        "chartable": True,
        "form": form,
        "axis": axis,
        "measures": measures,
        "dropped": dropped,
        "reason": "",
    }


def _choose_axis(
    frame: pd.DataFrame,
    temporal: list[str],
    periodic: list[str],
    categorical: list[str],
) -> str | None:
    """Pick the column to put on the axis, or None to decline the chart.

    TWO RULES, IN THIS ORDER, AND THE ORDER IS THE WHOLE POINT.

    1. Prefer a column with one row per value. A chart axis wants one mark per
       category; a column that repeats is a series key, not an axis.
    2. Among the candidates, take the one that appears FIRST in the result.

    Rule 2 works because SQL convention puts the grouping key at the head of the
    SELECT list, so the first eligible column is almost always the thing the
    question was grouped by.

    This replaced a rule of "a time column always wins", which was wrong in a way
    that produced a confidently nonsensical chart. Asked how average August
    temperature changed each year, the model returned
    `year, raw_avg, avg_temp_f, yoy_change_f, obs_count, min_date, max_date` --
    the last two being diagnostics for the reader. "Temporal wins" picked
    `min_date`, so the x-axis became the start of each August rather than the
    year, and eight points landed almost on top of each other. The intended axis,
    `year`, was sitting in column 0 the whole time.
    """
    candidates = [c for c in frame.columns if c in temporal or c in periodic or c in categorical]
    if not candidates:
        return None

    unique = [c for c in candidates if frame[c].nunique(dropna=False) == len(frame)]
    if unique:
        return unique[0]

    # Nothing is one-row-per-value. Still chartable if there is exactly one label
    # column (repeated values usually mean the query returned raw rows), but with
    # several it is guesswork, so decline and let the table speak.
    if len(candidates) == 1:
        return candidates[0]

    named = [c for c in candidates if "name" in c.lower()]
    return named[0] if named else None


def _shared_axis_measures(
    frame: pd.DataFrame, numeric: list[str]
) -> tuple[list[str], list[str]]:
    """Pick the measures that can honestly share one y-axis.

    Anchored on the FIRST numeric column rather than the largest, because column
    order follows the SELECT list and the first measure is the one the question
    was about. Anchoring on the largest would let an incidental big number evict
    the subject of the query from its own chart.
    """
    if len(numeric) <= 1:
        return numeric, []

    def magnitude(name: str) -> float:
        values = frame[name].abs()
        top = values.max()
        return float(top) if pd.notna(top) and top > 0 else 0.0

    anchor = magnitude(numeric[0])
    if anchor == 0:
        return numeric[:1], numeric[1:]

    keep, dropped = [numeric[0]], []
    for name in numeric[1:]:
        size = magnitude(name)
        ratio = max(size, anchor) / max(min(size, anchor), 1e-9)
        if size > 0 and ratio <= COMPARABLE_MAGNITUDE_RATIO:
            keep.append(name)
        else:
            dropped.append(name)

    # Slot order is the CVD-safety mechanism, so never plot more series than the
    # palette has validated slots.
    if len(keep) > len(theme.CATEGORICAL_LIGHT):
        dropped.extend(keep[len(theme.CATEGORICAL_LIGHT):])
        keep = keep[: len(theme.CATEGORICAL_LIGHT)]

    return keep, dropped


def figure(frame: pd.DataFrame, chart_plan: dict) -> go.Figure:
    """Draw the planned chart."""
    dark = theme.is_dark()
    tokens = theme.tokens(dark)
    palette = tokens["cat"]
    axis = chart_plan["axis"]
    measures = chart_plan["measures"]
    single = len(measures) == 1

    fig = go.Figure()

    if chart_plan["form"] == "line":
        data = frame.sort_values(axis)
        for index, measure in enumerate(measures):
            fig.add_trace(
                go.Scatter(
                    x=data[axis],
                    y=data[measure],
                    name=measure,
                    mode="lines",
                    line={"width": 2, "color": palette[index]},
                    hovertemplate=f"%{{x}}<br>{measure}: %{{y:,.2f}}<extra></extra>",
                )
            )
        # automargin, for the same reason the horizontal-bar path below sets it:
        # theme.layout's 8px margins are deliberately tight, and Plotly does not
        # widen them to fit tick labels -- it truncates them. On a line chart
        # that clipped every y-axis value down to its last digit and cut the date
        # labels off the bottom edge. Set here rather than in theme.py, which is
        # kept identical to the weblog dashboard's copy.
        layout = theme.layout(dark)
        layout["xaxis"]["automargin"] = True
        layout["yaxis"]["automargin"] = True
        fig.update_layout(**layout)
        fig.update_xaxes(title_text=axis)
        fig.update_yaxes(title_text=measures[0] if single else None)
        return fig

    # Horizontal bars. Station names and airport names are long, and horizontal
    # bars give them room to be read without rotating the labels 45 degrees.
    #
    # THE ROW ORDER IS THE QUERY'S, NOT RE-SORTED. This was originally
    # `sort_values(measures[0])`, which quietly contradicted the answer: asked for
    # the stations furthest ABOVE their August normal, the model wrote
    # `ORDER BY diff_f DESC`, but `diff_f` is dropped from the chart for scale
    # reasons, so sorting by the first plotted measure put the hottest station on
    # top instead of the most anomalous one. The chart ranked by one thing while
    # the text ranked by another.
    #
    # The SELECT's ORDER BY *is* the intent, including when the column it sorts on
    # is not drawn. Only the reversal below is applied, because Plotly renders the
    # first category at the BOTTOM of a horizontal bar chart and a top-N should
    # read from the top.
    data = frame.iloc[::-1]
    height = max(280, 26 * len(data) * max(1, len(measures)) + 90)

    for index, measure in enumerate(measures):
        fig.add_trace(
            go.Bar(
                y=data[axis].astype(str),
                x=data[measure],
                name=measure,
                orientation="h",
                marker={
                    "color": palette[index],
                    # 4px rounded data-end, anchored at the baseline.
                    "cornerradius": 4,
                    # 2px surface gap so adjacent bars never touch.
                    "line": {"width": 2, "color": tokens["surface"]},
                },
                # Direct value labels. These are also the relief the light
                # palette's sub-3:1 contrast WARN requires — see theme.py.
                text=[f"{v:,.1f}" if pd.notna(v) else "" for v in data[measure]],
                textposition="outside",
                textfont={"color": tokens["text_2"], "size": 11},
                cliponaxis=False,
                hovertemplate=f"%{{y}}<br>{measure}: %{{x:,.2f}}<extra></extra>",
            )
        )

    layout = theme.layout(dark, height=height)
    # Grid on the measure axis only; a grid across the category axis adds lines
    # that encode nothing.
    layout["yaxis"]["gridcolor"] = "rgba(0,0,0,0)"
    # automargin is REQUIRED for horizontal bars, not a nicety. The category axis
    # here carries names like "SANTA BARBARA MUNICIPAL AIRPORT", and the layout's
    # 8px left margin clipped every one of them down to a single character. Plotly
    # does not widen the margin on its own; it just truncates.
    layout["yaxis"]["automargin"] = True
    layout["xaxis"]["automargin"] = True
    fig.update_layout(**layout, barmode="group", bargap=0.28, bargroupgap=0.08)
    fig.update_xaxes(title_text=measures[0] if single else None)
    # A single series needs no legend — the axis title names it.
    fig.update_layout(showlegend=not single)
    return fig

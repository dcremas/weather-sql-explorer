"""Palette and Plotly template for the SQL explorer.

The values are lifted verbatim from the weblog dashboard's `theme.py`, on purpose:
these two apps sit on the same domain behind the same nginx, and a visitor moving
between them should not meet two different-looking chart styles. Copying the
numbers also means copying the validation, so both were re-run here.

Re-validated 2026-08-20 with the dataviz validator, against the surfaces below:

  light  ALL CHECKS PASS  — worst adjacent CVD dE 9.1 (protan), normal-vision 19.6
                            WARN: 3 slots below 3:1 contrast vs surface
  dark   ALL CHECKS PASS  — worst adjacent CVD dE 8.4 (protan), normal-vision 19.3
                            contrast: all 8 above 3:1

THE LIGHT-MODE CONTRAST WARN IS DISCHARGED, NOT IGNORED. A sub-3:1 slot obliges
either visible labels or a table view. This app always renders the full result
table beneath the chart, and bars carry direct value labels, so both reliefs are
present. If the table is ever made collapsible-by-default, that relief is gone
and this needs revisiting.

Slot ORDER is the colorblind-safety mechanism, not decoration: assign series to
slots in order and never cycle past slot 8. This app charts at most a handful of
measures at once, so it never approaches that -- but the rule is why the list is
not sorted or shuffled for looks.
"""

from __future__ import annotations

# --- categorical: assign in order, never cycle ------------------------------
CATEGORICAL_LIGHT = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]
CATEGORICAL_DARK = [
    "#3987e5", "#d95926", "#199e70", "#c98500",
    "#d55181", "#008300", "#9085e9", "#e66767",
]

_LIGHT = {
    "surface": "#fcfcfb",
    "text": "#0b0b0b",
    "text_2": "#52514e",
    "text_muted": "#7a7873",
    "grid": "#e8e7e3",
    "axis": "#d5d4cf",
    "cat": CATEGORICAL_LIGHT,
}
_DARK = {
    "surface": "#1a1a19",
    "text": "#ffffff",
    "text_2": "#c3c2b7",
    "text_muted": "#8d8c83",
    "grid": "#2e2e2c",
    "axis": "#3d3d3a",
    "cat": CATEGORICAL_DARK,
}


def tokens(dark: bool) -> dict:
    """Surface/ink/series tokens for the active mode.

    Dark is a selected set of steps for the dark surface, not an automatic flip
    of the light values -- flipping produces hues that fail the contrast check.
    """
    return _DARK if dark else _LIGHT


def is_dark() -> bool:
    """Whether the viewer is in dark mode, defaulting to light when unknown.

    `st.context.theme.type` returns None outside a script run and on older
    Streamlit builds, so it is read defensively. Light is the safer default: its
    palette carries the contrast WARN that the table view already discharges,
    whereas guessing dark on a light page would paint light-surface hues.
    """
    try:
        import streamlit as st

        return st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001 - never take the page down over a colour
        return False


def layout(dark: bool, height: int = 380) -> dict:
    """Plotly layout for the active mode.

    Grid and axes are deliberately recessive: the marks carry the data, and a
    grid competing with them is the most common way a chart gets noisy.
    """
    t = tokens(dark)
    return {
        "height": height,
        "paper_bgcolor": t["surface"],
        "plot_bgcolor": t["surface"],
        "font": {"color": t["text_2"], "size": 12},
        "margin": {"l": 8, "r": 16, "t": 8, "b": 8},
        "xaxis": {
            "gridcolor": t["grid"],
            "linecolor": t["axis"],
            "zerolinecolor": t["axis"],
            "title": {"font": {"color": t["text_muted"], "size": 11}},
        },
        "yaxis": {
            "gridcolor": t["grid"],
            "linecolor": t["axis"],
            "zerolinecolor": t["axis"],
            "title": {"font": {"color": t["text_muted"], "size": 11}},
        },
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "x": 0,
            "font": {"color": t["text_2"], "size": 11},
        },
        "hoverlabel": {"font": {"size": 12}},
    }

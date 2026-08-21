"""Page furniture: the stylesheet, and the primitives the diagrams are drawn with.

NOTHING HERE KNOWS WHICH COLOUR MODE THE PAGE IS IN, ON PURPOSE
---------------------------------------------------------------
The first version of this file took `dark: bool` from `theme.is_dark()` and baked
matching hex values into the stylesheet and into every SVG element. It was wrong,
and the way it was wrong is worth recording, because the obvious design has the
bug built into it.

`theme.is_dark()` reads `st.context.theme.type`, which is a value from the last
script run. A visitor switching Light/Dark in Streamlit's own menu changes the
page's colours in the browser WITHOUT triggering a rerun -- so the chrome went
dark while this file's colours stayed light, and the page rendered near-black
body text on a near-black background. Verified in a browser: unreadable until
some later interaction happened to rerun the script.

So every colour below is derived, in the browser, from the colour Streamlit has
already resolved correctly:

    currentColor                        the theme's own text colour
    color-mix(… currentColor N% …)      a tint, or a muted step, of it

That is reactive by construction. There is no snapshot to go stale and no second
palette to keep in step with theme.py.

TWO CONSEQUENCES WORTH KNOWING
------------------------------
1. `ACCENT` is a SINGLE blue for both modes. No blue reaches 4.5:1 against both a
   near-white and a near-black surface -- that is geometry, not a bad pick -- so
   the accent is used for borders, rules and diagram strokes, where 3:1 is the
   bar, and never for small text. The "STEP 1" eyebrows are muted grey rather
   than blue for exactly this reason. Measured: 3.96:1 on the light surface,
   4.29:1 on the dark one.

2. Every `color-mix` rule is paired with a plain fallback that degrades to
   legible rather than to invisible. For text that means inheriting the theme
   colour at full strength -- less pretty, still readable. For SVG it means an
   `rgba(128,128,128,…)` value that works on either surface.

WHY THERE IS ANY HAND-WRITTEN CSS
---------------------------------
Most of the look is set in `.streamlit/config.toml`, which is the supported route
and survives upgrades. This file holds only what config.toml has no token for,
and every selector is one of:

  * a class on markup THIS FILE emits (`.sqlx-*`, `.sx-*`) -- fully ours;
  * `.st-key-<key>`, the documented hook Streamlit puts on any element given a
    `key=`, which is API rather than an implementation detail;
  * exactly two `data-testid` selectors, for the page's top padding and the tab
    bar. Both cosmetic: if a release renames them the rules stop applying and the
    page reverts to Streamlit defaults. Nothing breaks.

If you find yourself reaching for a third testid, check config.toml first.

WHY THE DIAGRAMS ARE HAND-WRITTEN SVG
-------------------------------------
The two diagrams -- the request pipeline in guide.py, the entity map in
data_model.py -- are the explanation for a visitor who has never seen
text-to-SQL, so they have to stay legible in both colour modes and on a phone.
Inline SVG styled by the classes below gets that for free, with no image asset to
re-export when a colour changes. Callers pass geometry and a class name; they do
not pass colours.
"""

from __future__ import annotations

import html

import streamlit as st

# One accent for both colour modes -- see consequence 1 above. Also the value
# `.streamlit/config.toml` sets as primaryColor, so chrome and diagrams agree.
ACCENT = "#2f7fda"

# NEVER WRITE AN ANGLE-BRACKETED TAG NAME INSIDE THIS STYLESHEET, NOT EVEN IN A
# COMMENT. Streamlit runs `st.html` content through DOMPurify, and a `<style>`
# element whose text contains something that parses as a tag is discarded
# WHOLESALE -- not the offending line, the entire element. Two words in two CSS
# comments here, describing which HTML elements a rule applied to, silently
# deleted all 9KB of this file's styling: the page rendered as unstyled markup
# with no error, no console warning, and nothing missing from the Python side.
# Bisected to confirm. Write element names as bare words instead.
#
# ACCENT_COLOR is substituted for `ACCENT` at inject time, so the accent is
# defined in exactly one place in this file.
_CSS = """
<style>
/* --- Streamlit chrome ------------------------------------------------- */
/* 4.75rem, and there is a floor under this. `[data-testid="stHeader"]` is an
   absolutely positioned, OPAQUE 56px strip at z-index 999990: it does not
   reserve space, it paints over whatever is beneath it. 71px puts the page title
   15px clear. An earlier attempt at 2.4rem looked correct in the DOM -- heading
   present, right size, right offset -- and the only symptom was that its top
   half was invisible. Measure before lowering this; a half-painted heading reads
   as a font bug rather than a layout one.

   This is also why app.py keeps `st.chat_input` inside the Ask tab. From the
   main body it wraps the page in `stAppScrollToBottomContainer`, which scrolls
   the top of the content above the viewport on every rerun and puts the masthead
   back under this overlay whatever value is set here. See constraint 5 there. */
[data-testid="stMainBlockContainer"] { padding-top: 4.75rem; }

/* Tab labels are 14px by default; these three tabs are the page's primary
   navigation and read as an afterthought at that size. */
[data-testid="stTabs"] button[role="tab"] p { font-size: 1rem; font-weight: 600; }

/* --- hero ------------------------------------------------------------- */
.sqlx-hero { margin: 0 0 1.1rem 0; }
.sqlx-hero h1 {
    font-size: 2.1rem; font-weight: 700; line-height: 1.15;
    letter-spacing: -0.02em; margin: 0 0 0.5rem 0; color: inherit;
}
/* Not muted. Muting a paragraph that holds a `strong` mutes the strong text
   too, and the emphasis inverts. Full-strength text with weight for emphasis is
   both simpler and more legible. */
.sqlx-hero .sqlx-lede {
    font-size: 1.06rem; line-height: 1.55; max-width: 62ch;
    margin: 0; color: inherit;
}
.sqlx-hero .sqlx-lede strong { font-weight: 640; }

/* --- fact chips ------------------------------------------------------- */
.sqlx-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.95rem 0 0 0; }
.sqlx-chip {
    display: inline-flex; align-items: baseline; gap: 0.4rem;
    border: 1px solid rgba(128,128,128,0.34); border-radius: 999px;
    padding: 0.24rem 0.7rem; font-size: 0.82rem; line-height: 1.4;
    background: rgba(128,128,128,0.08); white-space: nowrap;
}
.sqlx-chip b { font-weight: 650; font-variant-numeric: tabular-nums; }

/* --- the four-step strip: the whole idea, above the fold -------------- */
.sqlx-steps {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 0.55rem; margin: 0.3rem 0 0.5rem 0;
}
@media (max-width: 820px) { .sqlx-steps { grid-template-columns: repeat(2, 1fr); } }
.sqlx-step {
    border: 1px solid rgba(128,128,128,0.30); border-radius: 0.6rem;
    padding: 0.6rem 0.75rem 0.7rem; background: rgba(128,128,128,0.07);
}
/* Grey, not accent. See consequence 1: this is ~10px bold, which needs 4.5:1,
   and no single blue clears that on both surfaces. */
.sqlx-step .sqlx-step-n {
    font-size: 0.68rem; font-weight: 700; letter-spacing: 0.09em;
    text-transform: uppercase;
}
.sqlx-step h4 {
    font-size: 0.9rem; font-weight: 640; margin: 0.15rem 0 0.2rem 0;
    line-height: 1.3; color: inherit;
}
.sqlx-step p { font-size: 0.8rem; line-height: 1.4; margin: 0; }

/* --- example question buttons ----------------------------------------- */
/* Streamlit centres button labels, which reads badly for a wrapped sentence and
   makes six of them look like a wall. Keyed container, so this survives
   upgrades: the key is API, the class it produces is documented. */
.st-key-sqlx-examples button {
    justify-content: flex-start; text-align: left; height: 100%;
    min-height: 3.5rem; padding: 0.6rem 0.8rem; align-items: flex-start;
}
.st-key-sqlx-examples button p {
    text-align: left; font-size: 0.86rem; line-height: 1.35; font-weight: 450;
}

/* --- diagrams --------------------------------------------------------- */
.sqlx-figure { margin: 0.2rem 0 0.4rem 0; overflow-x: auto; }
.sqlx-figure svg { display: block; height: auto; }
.sqlx-caption { font-size: 0.8rem; line-height: 1.5; margin: 0.15rem 0 0 0; }

/* --- prose blocks in the explainer tabs ------------------------------- */
/* Prefix match on the keyed-container class, so one rule covers every
   prose(key="sqlx-prose-…") call without a selector per block. */
[class*="st-key-sqlx-prose"] { max-width: 74ch; }
[class*="st-key-sqlx-prose"] p, [class*="st-key-sqlx-prose"] li { line-height: 1.62; }

/* --- a callout that is not an st.info box ----------------------------- */
.sqlx-callout {
    border: 1px solid rgba(128,128,128,0.30);
    border-left: 3px solid ACCENT_COLOR;
    border-radius: 0.45rem; padding: 0.75rem 0.95rem; margin: 0.7rem 0;
    background: rgba(128,128,128,0.07);
}
.sqlx-callout h4 {
    font-size: 0.88rem; font-weight: 660; margin: 0 0 0.3rem 0; color: inherit;
}
/* Full strength, not muted: these paragraphs carry inline bold for emphasis. */
.sqlx-callout p { font-size: 0.87rem; line-height: 1.58; margin: 0; color: inherit; }
.sqlx-callout p + p { margin-top: 0.45rem; }
.sqlx-callout code {
    font-size: 0.85em; padding: 0.05rem 0.28rem; border-radius: 0.25rem;
    background: rgba(128,128,128,0.16);
}

/* --- SVG element roles ------------------------------------------------ */
/* All geometry comes from the caller; all colour comes from here. */
.sx-panel { fill: rgba(128,128,128,0.05); stroke: rgba(128,128,128,0.20); }
.sx-box { fill: rgba(128,128,128,0.09); stroke: rgba(128,128,128,0.34); stroke-width: 1; }
.sx-box-fact { fill: rgba(47,127,218,0.13); stroke: ACCENT_COLOR; stroke-width: 1.7; }
.sx-foreign { stroke-dasharray: 5 3; }
.sx-seam { fill: none; stroke: ACCENT_COLOR; stroke-width: 1.2; stroke-dasharray: 6 4; }
.sx-edge { fill: none; stroke: rgba(128,128,128,0.55); stroke-width: 1.3; }
.sx-edge-key { fill: none; stroke: ACCENT_COLOR; stroke-width: 2.2; }

.sx-text { fill: currentColor; font-family: system-ui,-apple-system,Segoe UI,sans-serif; }
.sx-mono { font-family: ui-monospace,SFMono-Regular,Menlo,monospace; }
.sx-head { font-size: 13px; font-weight: 660; }
.sx-sub { font-size: 11px; fill: rgba(128,128,128,0.95); }
.sx-title { font-size: 12px; font-weight: 660; }
.sx-title-fact { font-size: 13px; font-weight: 660; }
.sx-meta { font-size: 10px; fill: rgba(128,128,128,0.95); }
.sx-cols { font-size: 9.5px; fill: rgba(128,128,128,1); }
.sx-label { font-size: 10px; fill: rgba(128,128,128,0.95); }
.sx-label-key { font-size: 11px; font-weight: 700; fill: ACCENT_COLOR; }
.sx-seam-label { font-size: 11px; font-weight: 650; fill: ACCENT_COLOR; }
.sx-note { font-size: 9.5px; font-style: italic; fill: rgba(128,128,128,0.95); }
.sx-legend { font-size: 10.5px; fill: rgba(128,128,128,0.95); }

/* pipeline diagram */
.sx-stage-title { font-size: 13.5px; font-weight: 650; }
.sx-stage-sub { font-size: 11.5px; fill: rgba(128,128,128,0.95); }
.sx-flow { font-size: 11.5px; fill: rgba(128,128,128,1); }
.sx-rail { font-size: 11px; font-weight: 600; fill: ACCENT_COLOR; }

/* --- the color-mix upgrade -------------------------------------------- */
/* Everything above degrades to legible without this block. Where color-mix is
   supported -- which is everywhere current -- these rules replace the neutral
   greys with true tints of the theme's own text colour, so the page and the
   diagrams track a Light/Dark switch with no rerun at all. */
@supports (color: color-mix(in srgb, red, blue)) {
    .sqlx-chip {
        border-color: color-mix(in srgb, currentColor 26%, transparent);
        background: color-mix(in srgb, currentColor 6%, transparent);
    }
    .sqlx-chip span { color: color-mix(in srgb, currentColor 62%, transparent); }
    .sqlx-step {
        border-color: color-mix(in srgb, currentColor 22%, transparent);
        background: color-mix(in srgb, currentColor 5%, transparent);
    }
    .sqlx-step .sqlx-step-n { color: color-mix(in srgb, currentColor 55%, transparent); }
    .sqlx-step p { color: color-mix(in srgb, currentColor 68%, transparent); }
    .sqlx-caption { color: color-mix(in srgb, currentColor 62%, transparent); }
    .sqlx-callout {
        border-color: color-mix(in srgb, currentColor 22%, transparent);
        background: color-mix(in srgb, currentColor 5%, transparent);
        border-left-color: ACCENT_COLOR;
    }
    .sqlx-callout code { background: color-mix(in srgb, currentColor 12%, transparent); }

    .sx-panel {
        fill: color-mix(in srgb, currentColor 3.5%, transparent);
        stroke: color-mix(in srgb, currentColor 14%, transparent);
    }
    .sx-box {
        fill: color-mix(in srgb, currentColor 6%, transparent);
        stroke: color-mix(in srgb, currentColor 26%, transparent);
    }
    .sx-box-fact { fill: color-mix(in srgb, ACCENT_COLOR 12%, transparent); }
    .sx-edge { stroke: color-mix(in srgb, currentColor 34%, transparent); }
    .sx-sub, .sx-meta, .sx-label, .sx-note, .sx-legend, .sx-stage-sub {
        fill: color-mix(in srgb, currentColor 58%, transparent);
    }
    .sx-cols, .sx-flow { fill: color-mix(in srgb, currentColor 78%, transparent); }
}
</style>
"""


def inject() -> None:
    """Put the stylesheet on the page. Call once, before anything is rendered.

    Takes no theme argument, and must not grow one -- see the module docstring.
    `st.html` is correct here: DOMPurify's html profile keeps `<style>`. It is
    only SVG that it strips, which is why `figure` uses markdown instead.
    """
    st.html(_CSS.replace("ACCENT_COLOR", ACCENT))


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


def hero(title: str, lede_html: str, chips: list[tuple[str, str]]) -> None:
    """Page masthead: name, one paragraph of what this is, and the hard facts.

    `lede_html` is trusted markup from this repo, not user input. `chips` are
    (value, label) pairs and are escaped.
    """
    rendered = "".join(
        f'<span class="sqlx-chip"><b>{html.escape(value)}</b>'
        f"<span>{html.escape(label)}</span></span>"
        for value, label in chips
    )
    st.html(
        f'<div class="sqlx-hero"><h1>{html.escape(title)}</h1>'
        f'<p class="sqlx-lede">{lede_html}</p>'
        f'<div class="sqlx-chips">{rendered}</div></div>'
    )


def steps(items: list[tuple[str, str]]) -> None:
    """The numbered strip explaining the pipeline in one glance.

    This is the only explanation a visitor is guaranteed to see: it sits above
    the question box on the default tab, unfolded, because someone who has never
    met text-to-SQL should not have to open anything to find out what the page
    does. The long version lives in guide.py.
    """
    cards = "".join(
        f'<div class="sqlx-step"><div class="sqlx-step-n">Step {n}</div>'
        f"<h4>{html.escape(heading)}</h4><p>{html.escape(body)}</p></div>"
        for n, (heading, body) in enumerate(items, start=1)
    )
    st.html(f'<div class="sqlx-steps">{cards}</div>')


def callout(heading: str, *paragraphs: str) -> None:
    """An aside. Not `st.info`, because these are asides and not status.

    Paragraph text is trusted markup from this repo so a little inline `<code>`
    and `<b>` can be used; nothing user-supplied reaches here.
    """
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    st.html(f'<div class="sqlx-callout"><h4>{html.escape(heading)}</h4>{body}</div>')


def figure(svg: str, caption: str | None = None, max_width: int | None = None) -> None:
    """Render an inline SVG diagram, scrollable rather than squashed when narrow.

    `st.markdown(unsafe_allow_html=True)`, NOT `st.html`, and this is the whole
    reason this function exists rather than being inlined at its two call sites.
    Streamlit sanitises both with DOMPurify, but `st.html` runs it with
    `USE_PROFILES: {html: true}` -- a profile that does not include SVG, so every
    `<svg>` element is removed. The failure is silent and total: the caption
    renders, the wrapper div renders, and the diagram is simply gone, with
    nothing in the console. Verified against Streamlit 1.62 by probing both
    routes.

    `max_width` caps the rendered size. The SVG is width="100%", so without a cap
    a diagram drawn on a narrow canvas is scaled UP to fill the column and its
    labels come out oversized. The fix for a diagram with dead space on one side
    is a narrower canvas AND a cap, not one or the other.
    """
    tail = f'<p class="sqlx-caption">{caption}</p>' if caption else ""
    cap = f' style="max-width:{max_width}px"' if max_width else ""
    st.markdown(
        f'<div class="sqlx-figure"{cap}>{svg}</div>{tail}', unsafe_allow_html=True
    )


def prose(markdown: str, key: str) -> None:
    """Body copy at a readable measure.

    Streamlit's markdown fills the full column width, which on this wide layout
    is 120-plus characters a line. These tabs are the part of the app people
    actually have to read, so they get a measure instead.

    `key` must start with `sqlx-prose` -- that prefix is what the stylesheet
    matches on -- and must be unique on the page, like any Streamlit key.
    """
    assert key.startswith("sqlx-prose"), "prose() keys must carry the CSS prefix"
    with st.container(key=key):
        st.markdown(markdown)


# --------------------------------------------------------------------------- #
# SVG primitives
# --------------------------------------------------------------------------- #
#
# Every one of these takes a CSS class and no colour. The classes are defined in
# `_CSS` under "SVG element roles"; see the module docstring for why colour is
# not a parameter here and must not become one.


def svg_open(
    ident: str, width: int, height: int, title: str, desc: str, min_width: int
) -> str:
    """Opening tag for a responsive, described diagram.

    `role="img"` with a title and a desc is what a screen reader is handed
    instead of the picture, so both are required arguments rather than optional
    polish. `min_width` stops the labels collapsing into each other on a phone --
    the wrapper from `figure()` scrolls instead of squashing.

    `ident` is a caller-supplied slug for the title element's id. It is not
    derived from the title, because `hash()` is salted per process and would give
    the same diagram a different id on every restart.
    """
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'style="min-width:{min_width}px" role="img" '
        f'aria-labelledby="{ident}-title" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<title id="{ident}-title">{html.escape(title)}</title>'
        f"<desc>{html.escape(desc)}</desc>"
    )


def svg_box(x: int, y: int, w: int, h: int, cls: str = "sx-box", radius: int = 7) -> str:
    """A rounded rectangle.

    `cls` selects its role: `sx-box`, `sx-box-fact` or `sx-panel`, with
    `sx-foreign` added for a dashed border.
    """
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'class="{cls}"/>'
    )


def svg_text(
    x: int, y: int, content: str, cls: str, anchor: str = "start", rotate: int = 0
) -> str:
    """One line of label text. Content is escaped; callers pass plain strings.

    `cls` must include `sx-text` plus a role class, and `sx-mono` where a
    monospace face is wanted. `rotate` turns the label about its own anchor
    point, for labels running along a vertical rail.
    """
    turn = f' transform="rotate({rotate} {x} {y})"' if rotate else ""
    return (
        f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}"{turn}>'
        f"{html.escape(content)}</text>"
    )


def svg_arrow_defs(name: str, accent: bool = False) -> str:
    """Marker definition for arrow heads.

    A marker's fill cannot be `currentColor`: markers render in their own context,
    where `currentColor` does not resolve to the referring element's colour and
    the head comes out black. So the neutral head is a mid grey that reads on
    either surface, and the emphasised head is the accent.
    """
    fill = ACCENT if accent else "rgba(128,128,128,0.72)"
    return (
        f'<defs><marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{fill}"/></marker></defs>'
    )


def svg_path(d: str, cls: str = "sx-edge", marker: str | None = None) -> str:
    """An edge between two boxes, with an arrow head at the far end."""
    end = f' marker-end="url(#{marker})"' if marker else ""
    return f'<path d="{d}" class="{cls}"{end}/>'


def svg_join(d: str, marker: str) -> str:
    """A join: one line with an arrow head at BOTH ends, because it has no direction."""
    return (
        f'<path d="{d}" class="sx-edge-key" marker-start="url(#{marker})" '
        f'marker-end="url(#{marker})"/>'
    )

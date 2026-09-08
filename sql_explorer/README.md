# Weather Warehouse SQL Explorer

**Ask a weather question in English; get PostgreSQL, a chart, and the rows.**
Gemini 3.8 Flash writes the SQL, a Model Context Protocol server executes it as a
read-only role, and the query is shown next to the answer. Built 2026-08-20.

The web tier holds **no database credentials and builds no SQL**. Everything goes
through the MCP server in `../mcp_server`, so compromising this app does not put
the warehouse at risk — it was never trusted with it.

```
browser
  → nginx                TLS, per-IP rate limit
    → Streamlit           127.0.0.1:8503   this app
      → LangChain agent   Gemini 3.8 Flash
        → MCP server      127.0.0.1:8770   ../mcp_server, 3 of its 13 tools
          → Postgres      mcp_ro, SELECT on 10 tables, read-only txn, 30s timeout
```

---

## 0. What is on the page

Most visitors arrive from a portfolio link and have never seen a language model
write SQL. The explanation widens in three steps, and they can stop at any of
them:

| Where | What it says | Behind a click? |
|---|---|---|
| the hero paragraph | English in, SQL and rows out | no |
| the four-step strip | the whole pipeline, unfolded, above the question box | no |
| **How it works** tab | what text-to-SQL is, how it fails, how to check it | one tab |
| **The data model** tab | every readable table, every column, every unit | one tab |

Only the tabs are behind a click, and nothing on the landing view is
collapsible. An explanation a newcomer has to go looking for is one they will not
read, so the *concept* lives on the landing view and the *detail* lives in a tab.

**`guide.py`** is the newcomer explainer: a vertical pipeline diagram, one real
worked example (question → SQL → rows, with live numbers), the 151°F failure from
§3 told as a warning about trusting fluent output, and what to type. It reads
nothing and costs nothing.

**`data_model.py`** is the schema reference, and it is **read live from the
database** through the same `list_schema` and `describe_table` calls the model
itself makes — so the documentation a visitor reads and the documentation the
model reads are the same bytes, with no second copy to drift. It draws an entity
map of the ten tables, lists every column with the `COMMENT` that gives its unit,
shows the three join shapes worth knowing, and lists all 112 stations so a
visitor can check a place *before* spending one of their twelve questions on it.
No model is in that path, so browsing the schema is free and is not charged to
the ledger.

---

## 1. Running it locally

Both dependencies below fail in ways that look like app bugs, so `run.sh` checks
them first and tells you the fix rather than letting Streamlit start broken.

```bash
# 1. The SSH tunnel (public 5432 is closed — the tunnel is the only route)
launchctl kickstart -k gui/$(id -u)/com.dustincremascoli.pgtunnel

# 2. The MCP server, in its own shell and its own venv
cd ../mcp_server && ./.venv/bin/python -m weather_mcp.server --http

# 3. This app
./run.sh            # → http://127.0.0.1:8503
```

First-time setup:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # then fill in GOOGLE_API_KEY
```

---

## 2. The dependency pin that looks like a mistake and is not

`requirements.txt` pins **`mcp<2`** while `../mcp_server/requirements.txt` asks for
**`mcp>=2`**. Both are correct.

`langchain-mcp-adapters` 0.3.1 imports `RequestContext` from `mcp.shared.context`,
which the 2.x SDK moved, so installing `mcp>=2` here breaks tool discovery at
import time:

```
ImportError: cannot import name 'RequestContext' from 'mcp.shared.context'
```

These are two processes in two venvs that share a **wire protocol**, not a
library. A v1.29 client against a v2.0 server is verified working — 13 tools
discovered, `run_sql` executed. Do not unify them.

---

## 3. What the model can and cannot do

It gets three of the MCP server's thirteen tools: `list_schema`,
`describe_table`, `run_sql`. The typed tools (`rank_stations`,
`temperature_anomaly`, …) are deliberately withheld — they would answer many of
these questions more reliably, and that is the problem: the page would show a
tool name where the interesting thing is the query. Fewer tools also means a
smaller prompt on every turn, which matters on a public endpoint.

Every generated statement is parsed before execution (`../mcp_server/weather_mcp/guard.py`)
and rejected unless it is a single read-only `SELECT` over one of ten
allow-listed tables, with a `LIMIT` forced on. A rejection is displayed as a
warning with the query — it is the guard working, not a fault.

### Units are read from the database, not the prompt

`observations` columns are abbreviated and the units are not guessable: `tmp` is
already **Fahrenheit**, `slp` is **inches of mercury**. Those facts live as
`COMMENT`s in Postgres (`../mcp_server/comment_tables.sql`) and reach the model
through `describe_table`, so they cannot drift away from the schema.

This is not a theoretical risk. While building this, a hand-written
forecast-vs-history query applied a Celsius→Fahrenheit conversion to `tmp` and
reported August normals of **151°F** for Los Angeles. It looked like a reasonable
query and returned plausible-shaped nonsense.

---

## 4. Cost control

Public endpoint, real tokens. Four caps, in `budget.py` and the nginx config:

| Cap | Default | Closes |
|---|---|---|
| questions per session | 12 | one visitor looping all afternoon |
| tokens per day, global | 2,000,000 | many visitors, or a script, adding up |
| agent steps per question | 20 | a retry spiral — invisible to a question count |
| requests per IP | nginx `limit_req` | scripted hammering |

Measured cost is **~17,000–27,000 tokens per question**, so the daily cap is
roughly 80–110 questions. The ledger is a `flock`-guarded JSON file (not
`st.session_state`, which cannot see other sessions; not Postgres, because the
only role available is deliberately read-only). **It fails closed** — an
unreadable ledger pauses questions rather than assuming budget remains.

Failed questions are charged. A crashed or looping run still spent tokens, and
exempting failures is a hole in the cap.

---

## 5. Charting an unknown result set

`charts.py` decides *whether* to chart before *how*. The default is **no chart**:
a wrong auto-chart is worse than none, because a visitor cannot tell a bad chart
from a bad answer. A result earns one by having 2–40 rows, a usable axis, and a
measure that is not an id.

Two rules worth knowing, both written after seeing the alternative fail:

- **The axis is the first column with one row per value**, not "the date column".
  Asked how August temperature changed per year, the model returned `year, …,
  min_date, max_date` — the last two being reader diagnostics. A "temporal wins"
  rule put `min_date` on the x-axis and stacked eight points on top of each
  other; `year` was in column 0 all along.
- **Row order is the query's**, never re-sorted. `ORDER BY` is the intent, even
  when it sorts on a column too small in magnitude to plot. Re-sorting by the
  first plotted measure once ranked the chart by temperature while the text
  ranked by anomaly.

Never a dual axis. Measures share the y-axis only when their magnitudes are
within 10×; the rest are dropped from the chart, named on the page, and still in
the table.

---

## 6. Six things that break this app if changed

1. **`st.plotly_chart(..., theme=None)`.** The default `theme="streamlit"`
   silently repaints figures in Streamlit's palette, discarding `theme.py` and
   the CVD validation those colours were chosen to pass. The chart still looks
   fine, which is why it keeps coming back.
2. **Explicit `key=` on every chart and table.** Streamlit derives element ids
   from element type plus arguments, so the second answer on the page collided
   with the first and killed it with `StreamlitDuplicateElementId`. Keys are
   built from a monotonic exchange id, not list position — history is prepended,
   so positions shift and position-based keys would re-key everything on every
   question.
3. **`@st.cache_resource` on the agent.** Streamlit re-runs the script on every
   interaction; without the cache each question rebuilds the MCP client and
   abandons the previous connection.
4. **The result table is never collapsed.** It is the accessibility relief for
   the light palette's three sub-3:1 contrast slots (see `theme.py`), as well as
   the honest view of what the query returned. Tabs are fine — a chart and its
   table are always in the same one — but a chart with its table folded away is
   not.
5. **`st.chat_input` lives inside the Ask tab, not the main body.** This looks
   backwards: from the main body Streamlit pins the box to the bottom of the
   window, which is nicer. It also wraps the whole main area in
   `stAppScrollToBottomContainer` and scrolls it to the bottom on every rerun, so
   the masthead and the four-step strip sit above the viewport from the first
   paint and get painted over by the 56px `stHeader` overlay. The page title
   renders legibly half-erased. That is §0 undone for the sake of a pinned input,
   so the pin was what went.
6. **History is prepended, newest first.** That follows from 5: the question box
   is at the top of the Ask tab, so the answer belongs directly beneath it. If
   the input ever moves back to the bottom of the page, this has to flip with it.

---

## 7. Palette

Copied verbatim from the weblog dashboard's `theme.py` so the two apps on this
domain look like one system, and re-validated here against both surfaces:

```
light  ALL CHECKS PASS   worst adjacent CVD ΔE 9.1 (protan), normal-vision 19.6
                         WARN: 3 slots under 3:1 vs surface — discharged by the
                         always-visible table and direct value labels
dark   ALL CHECKS PASS   worst adjacent CVD ΔE 8.4 (protan), normal-vision 19.3
```

Slot order is the colourblind-safety mechanism: assign in order, never cycle.

---

## 8. Three Streamlit behaviours that fail silently

All three cost real time to find, because none of them produces an error, a
console warning, or anything missing on the Python side. Recorded so the next
person does not have to bisect them again.

**`st.html` deletes SVG.** Streamlit sanitises both `st.html` and
`st.markdown(unsafe_allow_html=True)` with DOMPurify, but `st.html` runs it with
`USE_PROFILES: {html: true}` — a profile that does not include SVG. Every
`<svg>` element is removed. The wrapper div renders, the caption renders, and the
diagram is simply gone. Both diagrams therefore go through `ui.figure`, which
uses markdown. `<style>` blocks and plain divs survive the html profile, so
everything else in `ui.py` can keep using `st.html`.

**A tag name in a CSS comment deletes the whole stylesheet.** A `<style>` element
whose text contains something that parses as a tag is discarded *wholesale* — not
the offending line, the element. Two words in two CSS comments, naming which HTML
elements a rule applied to, silently removed all 9KB of `ui.py`'s styling and the
page rendered as unstyled markup. Write element names as bare words.

**Theme is a server-side snapshot, and switching it does not rerun.**
`theme.is_dark()` reads `st.context.theme.type`, which is a value from the last
script run. A visitor switching Light/Dark in Streamlit's own menu repaints the
chrome in the browser *without* triggering a rerun — so an earlier version of
`ui.py`, which baked matching hex values into its stylesheet and its SVG, left
near-black text on a near-black background until some later interaction happened
to rerun the script. `ui.py` now derives every colour in the browser from
`currentColor` and `color-mix`, and takes no theme argument at all. `charts.py`
still reads the snapshot, which is why a Plotly figure lags a theme switch by one
interaction.

---

## 9. Layout

```
sql_explorer/
  app.py               Streamlit page and the ask/answer loop; the six
                       constraints in §6 live in its docstring
  ui.py                the stylesheet and the SVG primitives; knows nothing
                       about which colour mode the page is in, on purpose.
                       Also holds `SITES` -- one of seven copies of the
                       estate-wide footer row (canonical:
                       ../../prosite_flask/content.py; run
                       ../../check-footer-nav.sh after touching it)
  guide.py             the "How it works" explainer — see §0
  data_model.py        the "Data model" schema reference, read live — see §0
  agent.py             MCP client + Gemini + the background event loop.
                       `call_tool` reaches MCP with no model in the path,
                       which is how data_model.py reads the schema for free
  budget.py            the spend caps and the flock'd daily ledger
  charts.py            whether to chart an arbitrary result set, and how
  theme.py             the validated Plotly palette (charts only now)
  .streamlit/
    config.toml        theme tokens for both colour modes; the supported route
                       for anything it covers, so check it before adding CSS
  run.sh               local launcher; pre-checks the tunnel and the MCP server
  requirements.txt     note the deliberate `mcp<2` pin — see §2
  deploy/              systemd units, nginx vhost, provisioning -- note its
                       provision.sh/enable-tls.sh write nginx backups into
                       /etc/nginx/conf.d/ rather than the archive convention
                       the weblog scripts use (../../NGINX-RUNBOOK.md S1)
```

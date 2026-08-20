# Weather Warehouse SQL Explorer

**Ask a weather question in English; get PostgreSQL, a chart, and the rows.**
Gemini 3.7 Flash writes the SQL, a Model Context Protocol server executes it as a
read-only role, and the query is shown next to the answer. Built 2026-08-20.

The web tier holds **no database credentials and builds no SQL**. Everything goes
through the MCP server in `../mcp_server`, so compromising this app does not put
the warehouse at risk — it was never trusted with it.

```
browser
  → nginx                TLS, per-IP rate limit
    → Streamlit           127.0.0.1:8503   this app
      → LangChain agent   Gemini 3.7 Flash
        → MCP server      127.0.0.1:8770   ../mcp_server, 3 of its 13 tools
          → Postgres      mcp_ro, SELECT on 10 tables, read-only txn, 30s timeout
```

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
| agent steps per question | 14 | a retry spiral — invisible to a question count |
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

## 6. Four things that break this app if changed

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
   the honest view of what the query returned.

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

## 8. Layout

```
sql_explorer/
  app.py            Streamlit page; the four constraints above live in its docstring
  agent.py          MCP client + Gemini + the background event loop
  budget.py         the spend caps and the flock'd daily ledger
  charts.py         whether to chart an arbitrary result set, and how
  theme.py          the validated palette and Plotly layout
  run.sh            local launcher; pre-checks the tunnel and the MCP server
  requirements.txt  note the deliberate `mcp<2` pin — see §2
  deploy/           systemd units, nginx vhost, provisioning
```

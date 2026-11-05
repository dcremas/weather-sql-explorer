# Ask Weather — the weather warehouse SQL explorer

**Ask a weather question in English; get PostgreSQL, a chart, and the rows.**
Gemini 4 Argon (or another model, see §4) writes the SQL, a Model Context Protocol server executes it as a
read-only role, and the query is shown next to the answer. Built 2026-08-20.

The web tier holds **no database credentials and builds no SQL**. Everything goes
through the MCP server in `../mcp_server`, so compromising this app does not put
the warehouse at risk — it was never trusted with it.

```
browser
  → nginx                TLS, per-IP rate limit
    → Streamlit           127.0.0.1:8503   this app
      → LangChain agent   Gemini 4 Argon by default (SQLX_MODEL, llm.py)
        → MCP server      127.0.0.1:8770   ../mcp_server, 3 of its 13 tools
          → Postgres      mcp_ro, SELECT on 10 tables, read-only txn, 30s timeout
```

---

## 0. What is on the page

**Ask Football's layout, since 2026-11-05.** The two apps are the same idea pointed
at two warehouses, so they read as one: a single 960px column of cards on a dotted
page, light or dark by the visitor's OS. `style.py` and `estate.py` are copies of
`~/projects/pbp/explorer/`'s; keep them equal apart from the brand mark and the
wordmark. Top to bottom:

| Card | What it holds |
|---|---|
| header | wordmark and one sentence: English in, SQL and rows out |
| **Ask a question** | a scope line read live from `data_coverage` (stations, observations, the newest one), the question box, questions left and today's budget |
| example chips | six starter questions; a click asks |
| one card per answer | the answer, the chart, the SQL, the formatted rows, earlier attempts, and **Ask next** chips |
| **About the data** | six subject tiles, then collapsed: *Good to know* (plain-language caveats, `GOOD_TO_KNOW` in `app.py`), *How answers are made*, and *For SQL readers* (the tables, from `list_schema`) |
| footer | data credit, source link, the estate row |

Until 2026-11-05 the page had a sidebar, a hero, a four-step strip and three tabs
(Ask / How it works / The data model), with `ui.py`, `guide.py` and `data_model.py`
behind them. They are in git history; the entity diagram and the join recipes
went with `data_model.py`.

**What an answer looks like.** The system prompt (`SYSTEM_PROMPT` in `agent.py`)
fixes the shape, as Ask Football's does: **a bold one-sentence finding** with its
unit, two to four bullets (the sample behind an average, changes in degrees or in
percentage points), a `*Basis:*` line saying in plain words what was counted and
left out, and a `Next:` line that `app.py` lifts out and renders as Ask next chips.
The same prompt shapes the query for a reader: snake_case aliases with the unit
as a suffix (`avg_temp_f`, `max_wind_mph`), names rather than station codes, and an
all-station benchmark column on rankings. The result table is formatted from those
names (`_column_config`, labels from `charts.label`): "Avg temp (°F)", 8,712 not
8712, years without a separator, pressure and rain to two decimals. Only the
display changes; the CSV download is the raw data.

None of the About the data card calls the model, so it still works when answers
are paused. `data_coverage` is a page-only tool (`PAGE_TOOLS` in `agent.py`): the
page reads it, the model is never offered it.

---

## 1. Running it for development

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
cp .env.example .env && chmod 600 .env    # then fill in the key for SQLX_MODEL's provider
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
exempting failures is a hole in the cap. The one exception is the provider
refusing for billing (out of credit, or over a Console spend limit): that bought
nothing, and charging it would keep the page paused after a top-up.

### Switching models

`SQLX_MODEL` names the model and `llm.py` turns it into a chat model. A bare
`gemini-*` or `claude-*` name works as it always has; anything else is
`provider:model`:

| Provider | Setting | Key |
|---|---|---|
| Google (default) | `gemini-4-argon` | `GOOGLE_API_KEY` |
| Anthropic | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:gpt-6-astra` | `OPENAI_API_KEY` |
| OpenRouter (one key, most models) | `openrouter:qwen/qwen3.8-max` | `OPENROUTER_API_KEY` |
| DeepSeek | `deepseek:deepseek-flash` | `DEEPSEEK_API_KEY` |
| Qwen (Alibaba, international) | `qwen:<model>` | `DASHSCOPE_API_KEY` |
| Kimi (Moonshot) | `kimi:kimi-k3` | `MOONSHOT_API_KEY` |
| Mistral | `mistral:<model>` | `MISTRAL_API_KEY` |
| Together / Groq / Fireworks | `together:<model>` etc. | `TOGETHER_API_KEY` etc. |

Each provider reads its own key, so any number can sit in the env file and a
switch is one line:

```bash
# on the box: app.env overrides the unit's default, so no redeploy is needed
sudo vi /etc/sql-explorer/app.env      # e.g. ANTHROPIC_API_KEY=... and SQLX_MODEL=claude-sonnet-5-5
sudo systemctl restart sql-explorer
```

- **Nothing changes until the setting does.** Each provider's package is
  imported only when chosen, so the default path is the same code it always was.
- **`llm.py` has an identical twin** in the football explorer
  (`explorer/llm.py` in the play-by-play repository). Keep them in step by hand.
- **A Claude key is an API key from platform.claude.com.** A claude.ai Team or
  Pro plan does not include API access.
- **What each provider is sent.** Gemini: `temperature=0`, `low` effort. Claude:
  no temperature (current models reject one; Haiku 4.5 keeps `0`), `low` effort,
  refusal fallbacks, prompt caching. OpenAI: `low` effort on its reasoning
  models, and the library picks the Responses API where a model needs it. Every
  other provider: nothing optional -- untested parameters are where a 400 comes
  from, so they get their own defaults.
- **Outages are recognised for all of them** (`llm.refusal`): rate limits and
  overload say "try again in a minute"; an empty balance or spend limit says
  answers are paused, and is not charged.
- **Re-check the caps after a switch.** The measurements above are Gemini's.
  The ledger counts tokens and a token does not cost the same everywhere. Ask
  the Denver example before and after and compare.
- **Chinese-hosted endpoints.** `deepseek:`, `qwen:` and `kimi:` send visitors'
  questions to servers in China. The same open-weight models are on Together,
  Groq, Fireworks and OpenRouter if that matters.

### Comparing models: the test set

`eval/` holds 15 questions with answers computed from the warehouse on 2026-10-12,
each aimed at a known trap -- Fahrenheit treated as Celsius, `loc_subset`'s row per
year, hours counted as days, a place the warehouse does not cover. `eval/run_eval.py`
asks them through the app's own `agent.ask` and grades the answer text:

```bash
.venv/bin/python eval/run_eval.py gemini-3.8-flash claude-sonnet-5-5          # plan + cost ceiling, sends nothing
.venv/bin/python eval/run_eval.py gemini-3.8-flash claude-sonnet-5-5 --yes    # spends money
```

Needs the MCP server running and each model's key. It never touches the live
ledger. Results land in `eval/results/<timestamp>/report.md` (gitignored), with
every failed answer and its SQL in full -- read those before trusting a
percentage. Prices for the cost column are in `eval/prices.json`. `run_eval.py`
is a twin of the football explorer's; keep them in step. `provision.sh` leaves
`eval/` off the box.

**Rainfall was wrong until 2026-10-12.** The synoptic (FM-12) rows carried
overlapping multi-hour totals, so summing `prp` counted the same rain two or
three times (Seattle 2022: 89.8 inches against 40.98 official). The fix is in
the WeatherData repository (README, *Precipitation double counting*) and is applied
to the serving warehouse; the three rainfall questions here were measured after
it. A few stations are still wrong upstream -- Little Rock AFB, Shreveport 2025,
Duluth 2025 -- and the test set avoids them.

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

Never a dual axis. Measures share the y-axis only when they carry the **same
unit suffix** and their magnitudes are within 10×; the rest are dropped from the
chart, named on the page, and still in the table. The unit rule came first on Ask
Football, where a percentage shared an axis with an average in yards; here 75 °F
and 9 mph are within 10× and were drawn on one scale until 2026-11-05.

A measure with one value on every row (the all-station benchmark the prompt asks
rankings to carry) is drawn as a dashed reference line, not as a second set of
identical bars. Axis titles and legend entries come from `charts.label`, the same
function that names the table's columns.

---

## 6. Four things that break this app if changed

1. **`st.plotly_chart(..., theme=None)`.** The default `theme="streamlit"`
   silently repaints figures in Streamlit's palette, discarding `theme.py` and
   the CVD validation those colours were chosen to pass. The chart still looks
   fine, which is why it keeps coming back. `config.toml` deliberately sets no
   `chartCategoricalColors` (Ask Football's does), so a chart that loses
   `theme=None` looks wrong at once.
2. **Explicit `key=` on every chart and table.** Streamlit derives element ids
   from element type plus arguments, so the second answer on the page collided
   with the first and killed it with `StreamlitDuplicateElementId`. Keys are
   built from a monotonic answer id, not list position — history is prepended,
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

## 8. Three Streamlit behaviours that fail silently

All three cost real time to find, because none of them produces an error, a
console warning, or anything missing on the Python side. Recorded so the next
person does not have to bisect them again.

**`st.html` deletes SVG.** Streamlit sanitises both `st.html` and
`st.markdown(unsafe_allow_html=True)` with DOMPurify, but `st.html` runs it with
`USE_PROFILES: {html: true}` — a profile that does not include SVG. Every
`<svg>` element is removed, silently. The brand mark and the footer's new-tab icon
are therefore CSS data-URIs (`style.py`, `estate.py`).

**A tag name in a CSS comment deletes the whole stylesheet.** A `<style>` element
whose text contains something that parses as a tag is discarded *wholesale* — not
the offending line, the element. Two words in two CSS comments, naming which HTML
elements a rule applied to, once silently removed all 9KB of this app's styling.
Write element names as bare words.

**Theme is a server-side snapshot.** `theme.is_dark()` reads
`st.context.theme.type`, a value from the last script run. The page's own colours
follow the visitor's OS through a media query in `style.py`, so they switch at
once; a Plotly figure follows on the next rerun.

---

## 9. Layout

```
sql_explorer/
  app.py               the page, top to bottom, and the ask/answer loop; the
                       four constraints in §6 live in its docstring
  style.py             the stylesheet, the header, notices; a copy of Ask
                       Football's (~/projects/pbp/explorer/style.py)
  estate.py            the footer. Holds `SITES` -- one copy of the
                       estate-wide footer row (canonical:
                       ../../prosite_flask/content.py; run
                       ../../check-footer-nav.sh after touching it)
  agent.py             MCP client + the agent + the background event loop.
                       `call_tool` reaches MCP with no model in the path,
                       which is how the page reads the schema and the
                       coverage for free
  llm.py               SQLX_MODEL -> chat model, for every provider; twin of
                       play-by-play's explorer/llm.py
  budget.py            the spend caps and the flock'd daily ledger
  charts.py            whether to chart an arbitrary result set, and how;
                       `label` names columns for the chart and the table
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

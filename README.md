# Weather Warehouse SQL Explorer

**Ask a weather question in English. Get the PostgreSQL that answers it, a chart,
and the rows.**

Live demo: **https://sql.dustincremascoli.com**

Gemini 3.8 Flash writes the SQL. A Model Context Protocol server executes it as a
`SELECT`-only database role. The generated query is shown next to the answer, so
you can check the machine's work rather than trust it.

The data is 9.2M hourly observations from 112 US weather stations (NOAA GHCNh,
2019→present) joined against Apple WeatherKit forecasts for the same stations.

---

## The point: the web tier is never trusted with the database

```
browser
  └─ nginx                 TLS, per-IP connection limit
     └─ Streamlit          127.0.0.1:8503     sql_explorer/
        └─ LangChain agent Gemini 3.8 Flash
           └─ MCP server   127.0.0.1:8770     mcp_server/
              └─ Postgres  mcp_ro — SELECT only, read-only txn, 30s timeout
```

`sql_explorer/` holds **no database credentials and constructs no SQL string**.
Its only route to data is MCP tool calls, and the one tool that runs SQL puts it
through `mcp_server/weather_mcp/guard.py` first. Compromising the public web app
does not reach the warehouse, because the web app was never given anything to
reach it with.

That separation is why this repo contains two components instead of one.

## The two components

| Directory | What it is | Start at |
|---|---|---|
| **`sql_explorer/`** | The Streamlit app, the LangChain agent, the spend caps, the charting | [`sql_explorer/README.md`](sql_explorer/README.md) |
| **`mcp_server/`** | 13 read-only MCP tools over the warehouse; also usable standalone from Claude Desktop | [`mcp_server/README.md`](mcp_server/README.md) |

**They must stay siblings.** `sql_explorer/run.sh`, `agent.py`, and
`deploy/provision.sh` all reach the server as `../mcp_server`. Moving one without
the other breaks the app and the deploy.

`mcp_server/` is useful on its own — point Claude Desktop at it and you get
conversational access to the warehouse with no web tier at all. `sql_explorer/`
is not useful on its own.

## Quick start

```bash
# 1. Each component has its own venv; their `mcp` pins deliberately conflict.
#    See sql_explorer/README.md §2 for why that is correct and not a mistake.
for d in mcp_server sql_explorer; do
  (cd $d && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt)
done

# 2. Configure. Neither .env is committed.
cp mcp_server/.env.example   mcp_server/.env      # database connection
cp sql_explorer/.env.example sql_explorer/.env    # GOOGLE_API_KEY
chmod 600 mcp_server/.env sql_explorer/.env

# 3. Run the MCP server, then the app, in separate shells.
cd mcp_server   && ./.venv/bin/python -m weather_mcp.server --http
cd sql_explorer && ./run.sh          # → http://127.0.0.1:8503
```

`run.sh` pre-checks its dependencies and prints the fix, because both of them
fail in ways that look like application bugs.

## Cost control, because this is a public LLM endpoint

nginx **cannot** cap spend here, and the config looks like it can. Streamlit
sends every user action over one long-lived WebSocket, so a visitor loads the
page once and can then ask twenty questions without issuing another HTTP request
for `limit_req` to see.

The real cap is `sql_explorer/budget.py`: a per-session question limit and a
global daily token ceiling, kept in a `flock`'d JSON ledger, **failing closed**.
Failed runs are charged — exempting them would be a hole. Measured cost is
~17–27k tokens per question.

## Deploying

`sql_explorer/deploy/` provisions both components onto a single host as two
systemd units behind nginx, with TLS via certbot. See
[`sql_explorer/deploy/README-deploy.md`](sql_explorer/deploy/README-deploy.md).
Host-specific values appear as `<EC2_PUBLIC_IP>` — substitute your own.

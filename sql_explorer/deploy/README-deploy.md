# Deploying the SQL Explorer to lambda-playground-1

Two systemd services behind an nginx vhost, on the box that already runs four
sites plus the analytics dashboard. First deployed **2026-08-20**; last
redeployed **2026-08-21** (the explainer and schema tabs). Every command below
has been run and verified on the box.

```
nginx :443  sql.dustincremascoli.com
  └─ sql-explorer.service   127.0.0.1:8503   user sqlxapp     ← Google API key
       └─ weather-mcp.service 127.0.0.1:8770 user weathermcp  ← Postgres password
            └─ postgresql     127.0.0.1:5432 role mcp_ro
```

## Status

| Piece | State |
|---|---|
| `weather-mcp.service` | **running**, loopback 8770 |
| `sql-explorer.service` | **running**, loopback 8503 |
| nginx vhost | **installed, TLS** (`sqlx.conf`) |
| weblog map entry | **added** (`sql.dustincremascoli.com → sqlx`) |
| DNS A record | **done** — resolves to the EIP |
| TLS certificate | **issued**, Let's Encrypt, renews by certbot timer |
| Public reachability | **live** at https://sql.dustincremascoli.com |

Verified on the box: 13 MCP tools discovered over HTTP, a 9.18M-row count, a
cross-database FDW query, a rejected `DELETE`, and full Gemini answers. The
2026-08-21 redeploy additionally checked the three-tab page over HTTPS and the
live schema page against the box's own Postgres. All five existing sites
re-checked before and after.

`enable-tls.sh` is kept for a rebuild. It issues the certificate by webroot and
swaps `sqlx-http.conf` for `sqlx.conf`, and it is self-reverting: if the TLS
vhost fails `nginx -t` the previous file is restored and nginx is never reloaded
broken — a bad vhost takes down **all five** sites, not just this one. DNS is at
GoDaddy (no Route53 zone, no API credentials on the Mac), so an A record change
cannot be scripted and stays manual.

## Deploying a change

```bash
cd ~/projects/ec2-nginx/weather-sql-explorer
rsync -az --delete --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
      --exclude '.budget.json' --exclude '.git/' \
      mcp_server sql_explorer awsvm:/tmp/sqlx-deploy/
ssh awsvm 'sudo bash /tmp/sqlx-deploy/sql_explorer/deploy/provision.sh'
```

`awsvm` is the ~/.ssh/config alias for the box. Worth pre-flighting the new code
against the box's interpreter before restarting a live service — a syntax error
otherwise surfaces as a crash-loop on a public page:

```bash
ssh awsvm 'cd /tmp/sqlx-deploy/sql_explorer && /usr/bin/python3.11 -m py_compile *.py'
```

`provision.sh` is idempotent. It refreshes code, rebuilds venvs if the
interpreter changed, restarts both services, and **never overwrites an existing
credential file**.

## Six things that bit during this deploy

**1. `python3` means three different things on this box.** `ec2-user`'s is
3.13.9 — but it is a **pyenv shim under `/home/ec2-user`**, unusable by a system
service (wrong location, and `ProtectHome=true` blocks it). `root`'s is 3.9.25,
too old: every release of `mcp` needs ≥3.10, and a 3.9 venv fails with the
useless *"Could not find a version that satisfies the requirement mcp>=1.2 (from
versions: none)"*. The right one is **`/usr/bin/python3.11`**, which is also what
`weblog-dashboard` runs on. `provision.sh` names it explicitly and refuses to
proceed on anything older.

**2. `ProtectHome=true` makes Streamlit crash-loop at startup.** Streamlit probes
`$HOME/.streamlit/secrets.toml` before serving anything. The service user has no
home, so `$HOME` is `/home/sqlxapp`, which `ProtectHome` makes unreadable — so the
probe raises `PermissionError`, not the `FileNotFoundError` Streamlit handles
gracefully. It restarted five times and gave up. Fix: `Environment=HOME=/var/lib/sql-explorer`,
the `StateDirectory`. `weather-mcp` is unaffected; it never reads `HOME`.

**3. nginx must be reloaded immediately after `nginx -t`, not at the end.** The
reload used to sit after the service health checks, so when `sql-explorer` failed
to start the script exited first — leaving the vhost and the weblog map edit on
disk but **not in the running config**. The symptom is actively misleading:
requests fell through to the `default_server` and got a `301`, and the injected
beacon reported `data-site="other"`, so it looked like the vhost *and* the map
entry were both wrong when neither had ever been loaded.

**4. `--server.address=127.0.0.1` is load-bearing.** Streamlit otherwise binds
`0.0.0.0`, publishing 8503 on the public EIP — past nginx, past its rate limits,
past every security header. `provision.sh` asserts both ports are loopback-only
after start.

**5. `StartLimitIntervalSec`/`StartLimitBurst` go in `[Unit]`, not `[Service]`.**
In `[Service]` systemd logs *"Unknown key name … ignoring"* and applies its
5-starts-per-10s default, so `Restart=always` quietly is not always.
`weblog-collector.service` on this box has that bug — do not copy it.

**6. Two service users, deliberately.** `weathermcp` holds the Postgres
password; `sqlxapp` holds the Google API key. Neither can read the other's
environment file — verified both directions:

```
sudo -u sqlxapp     cat /etc/weather-mcp/mcp.env   → Permission denied
sudo -u weathermcp  cat /etc/sql-explorer/app.env  → Permission denied
```

So the public-facing process cannot obtain the database credential even if fully
compromised. Merging them would give that up for nothing.

## What nginx does *not* protect

Worth being blunt, because the config looks like it caps cost and does not.
Streamlit sends every user action over **one long-lived WebSocket**. A visitor
loads the page (an HTTP burst `limit_req` sees), then asks twenty questions over
that existing connection — generating **zero further HTTP requests**.

- `limit_req` → page-load hammering and scraping. Real, but not spend.
- `limit_conn 4` on `/_stcore/stream` → caps simultaneous sessions per IP, which
  is what makes a per-session question cap mean anything.
- **The spend cap is `budget.py`**: 10 questions per session, 1.5M tokens per day
  globally, failing closed. At ~17k tokens per question that is ~85 questions a
  day. If the bill looks wrong, tune that file — not this one.

## Memory

The binding constraint. The box has 3.8 GB, was at **1242 MB available** before
this deploy and **1098 MB** after, already ~800 MB into swap with nine other
services.

| | measured |
|---|---|
| `weather-mcp` | 69 MB |
| `sql-explorer` | 59 MB idle, 214 MB after serving questions |

Both units carry `MemoryHigh`/`MemoryMax` (240 MB / 480 MB) so a runaway result
set kills the demo rather than letting the kernel's OOM killer pick — on this box
its choice would likely be the website. `provision.sh` refuses to install below
600 MB available.

**This box is close to full.** The next service added here should be preceded by
a real look at `free -m`, not an assumption.

## Operating it

```bash
systemctl status weather-mcp sql-explorer
journalctl -u sql-explorer -f
journalctl -u weather-mcp -n 50

# Is the data path healthy? (runs 61 live checks)
# The `env $(systemctl show ...)` part is REQUIRED, not decoration: the database
# host and port live as Environment= lines in weather-mcp.service, while mcp.env
# holds only the password. Without them db.py falls back to its development
# default of port 15432 — the laptop's SSH tunnel — and every check fails with
# "connection refused" on a box where the database is fine.
sudo -u weathermcp env $(systemctl show weather-mcp -p Environment --value) \
  bash -c 'set -a; . /etc/weather-mcp/mcp.env; set +a
    cd /opt/weather-mcp && ./.venv/bin/python -m weather_mcp.selftest'

# Today's spend
sudo cat /var/lib/sql-explorer/budget.json

# Pause the demo without touching the database or the other sites
systemctl stop sql-explorer
```

To change a cap, edit `/etc/sql-explorer/app.env` and
`systemctl restart sql-explorer`.

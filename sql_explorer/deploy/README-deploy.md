# Deploying the SQL Explorer to lambda-playground-1

Two systemd services behind an nginx vhost, on the box that already runs four
sites plus the analytics dashboard. Deployed **2026-08-20**; every command below
was run and verified that day.

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
| nginx vhost | **installed, HTTP only** |
| weblog map entry | **added** (`sql.dustincremascoli.com → sqlx`) |
| DNS A record | **NOT DONE — manual, blocks everything below** |
| TLS certificate | not issued (needs DNS) |
| Public reachability | **none yet** |

Verified working on the box: 13 MCP tools discovered over HTTP, a 9,170,699-row
count, a cross-database FDW query, a rejected `DELETE`, and a full Gemini answer
in 17,394 tokens. All five existing sites re-checked before and after.

## Remaining steps, in order

**1. DNS (manual — this is the gate).** DNS is at GoDaddy: no Route53 zone, no
API credentials on the Mac, so this cannot be scripted.

```
sql.dustincremascoli.com   A   <EC2_PUBLIC_IP>
dig +short sql.dustincremascoli.com     # confirm before step 2
```

**2. TLS.**

```bash
sudo bash /tmp/sqlx-deploy/sql_explorer/deploy/enable-tls.sh
```

Issues the certificate by webroot and swaps `sqlx-http.conf` for `sqlx.conf`.
Self-reverting: if the TLS vhost fails `nginx -t` the previous file is restored
and nginx is never reloaded broken — a bad vhost takes down **all five** sites,
not just this one.

**3. The banner link** on the main site, once the URL is live.

## Deploying a change

```bash
cd ~/projects/WeatherData
rsync -az --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
      --exclude '.budget.json' --exclude '.git/' \
      mcp_server sql_explorer ec2-user@<EC2_PUBLIC_IP>:/tmp/sqlx-deploy/
ssh ec2-user@<EC2_PUBLIC_IP> 'sudo bash /tmp/sqlx-deploy/sql_explorer/deploy/provision.sh'
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
sudo -u weathermcp bash -c 'set -a; . /etc/weather-mcp/mcp.env; set +a
  cd /opt/weather-mcp && ./.venv/bin/python -m weather_mcp.selftest'

# Today's spend
sudo cat /var/lib/sql-explorer/budget.json

# Pause the demo without touching the database or the other sites
systemctl stop sql-explorer
```

To change a cap, edit `/etc/sql-explorer/app.env` and
`systemctl restart sql-explorer`.

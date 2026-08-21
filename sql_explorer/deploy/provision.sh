#!/usr/bin/env bash
# Provision the MCP server and the SQL explorer on lambda-playground-1.
#
# Run ON THE BOX as a user with sudo:
#   scp -r deploy ../../mcp_server ../../sql_explorer ec2-user@<EC2_PUBLIC_IP>:/tmp/sqlx-deploy/
#   ssh ec2-user@<EC2_PUBLIC_IP> 'sudo bash /tmp/sqlx-deploy/deploy/provision.sh'
#
# Idempotent: safe to re-run. Re-running refreshes the code and restarts both
# services, but never rewrites an existing credential file.
#
# HTTP ONLY. This deliberately stops before TLS, because the DNS A record is a
# manual step at GoDaddy (there is no Route53 zone and no API credentials on the
# Mac), and certbot cannot validate a host that does not resolve. Run
# enable-tls.sh once DNS is live.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
HOSTNAME_APP="${HOSTNAME_APP:-sql.dustincremascoli.com}"
WEBLOG_SITE="${WEBLOG_SITE:-sqlx}"

log() { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo"

# --- 0. Pre-flight ------------------------------------------------------------
# Memory is the real constraint on this box, not disk or CPU. Check before
# installing rather than discovering it when the OOM killer picks the website.
log "Pre-flight"
AVAIL_MB=$(free -m | awk '/^Mem:/ {print $7}')
echo "   available memory: ${AVAIL_MB} MB"
if (( AVAIL_MB < 600 )); then
    fail "only ${AVAIL_MB} MB available; these two services need ~300 MB.
Free something first — this box runs 9 services on 3.8 GB and is already using swap."
fi

systemctl is-active --quiet postgresql || fail "postgresql is not running"

# THE INTERPRETER MUST BE NAMED EXPLICITLY. Do not use bare `python3` here.
#
# Three different answers exist on this box:
#   ec2-user's `python3`  -> 3.13.9, but it is a PYENV SHIM under
#                            /home/ec2-user/.pyenv. Unusable for a system
#                            service: it lives in a home directory these units
#                            cannot read (ProtectHome=true), and the service
#                            users have no access to it at all.
#   root's `python3`      -> 3.9.25, the AL2023 system default. Too old: every
#                            release of `mcp` requires >=3.10, so a venv built
#                            with it fails with the deeply unhelpful
#                            "Could not find a version that satisfies the
#                            requirement mcp>=1.2 (from versions: none)".
#   /usr/bin/python3.11   -> 3.11.15. This is the one to use, and it is what
#                            weblog-dashboard on this box already runs on.
#
# provision.sh runs under sudo, so it gets root's 3.9 unless told otherwise.
PYTHON="${PYTHON:-/usr/bin/python3.11}"
[[ -x "$PYTHON" ]] || fail "no interpreter at ${PYTHON}.
Install one (dnf install python3.11) or set PYTHON= to a 3.10+ interpreter that
is NOT under a home directory."
PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYVER" in
    3.9|3.8|3.7|2.*) fail "${PYTHON} is ${PYVER}; mcp needs 3.10+" ;;
esac
echo "   interpreter: ${PYTHON} (${PYVER})"

# --- 1. Service users --------------------------------------------------------
# TWO users on purpose. weathermcp holds the Postgres password; sqlxapp holds the
# Google API key. The public-facing app therefore cannot read the database
# credential even if it is fully compromised. Do not merge them.
log "Service users"
for user in weathermcp sqlxapp; do
    if id "$user" >/dev/null 2>&1; then
        echo "   $user exists"
    else
        useradd --system --no-create-home --shell /sbin/nologin "$user"
        echo "   created $user"
    fi
done

# --- 2. Code ------------------------------------------------------------------
log "Code"
install_tree() {
    local src="$1" dest="$2" owner="$3"
    mkdir -p "$dest"
    # --delete keeps the target a mirror, but must never remove the venv or the
    # runtime state living inside it.
    rsync -a --delete \
          --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
          --exclude '.budget.json' \
          "$src/" "$dest/"
    chown -R "$owner:$owner" "$dest"
    echo "   $dest"
}
install_tree "$SRC/../../mcp_server"  /opt/weather-mcp  weathermcp
install_tree "$SRC/../../sql_explorer" /opt/sql-explorer sqlxapp

# --- 3. Virtualenvs ----------------------------------------------------------
# Separate venvs are REQUIRED, not tidiness: the MCP server needs `mcp>=2` and
# the app needs `mcp<2` (langchain-mcp-adapters 0.3.1 cannot import from the v2
# SDK). One shared venv cannot satisfy both. See sql_explorer/README.md §2.
log "Virtualenvs"
build_venv() {
    local dir="$1" owner="$2"
    # Rebuild from scratch if an existing venv was built with the wrong
    # interpreter -- a 3.9 venv cannot be upgraded in place, and leaving it
    # means every re-run fails the same way.
    if [[ -x "$dir/.venv/bin/python" ]]; then
        local have
        have=$("$dir/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        if [[ "$have" != "$PYVER" ]]; then
            echo "   $dir/.venv is python ${have}, rebuilding on ${PYVER}"
            rm -rf "$dir/.venv"
        fi
    fi
    if [[ ! -x "$dir/.venv/bin/python" ]]; then
        "$PYTHON" -m venv "$dir/.venv"
    fi
    "$dir/.venv/bin/pip" install --quiet --upgrade pip
    "$dir/.venv/bin/pip" install --quiet -r "$dir/requirements.txt"
    chown -R "$owner:$owner" "$dir/.venv"
    echo "   $dir/.venv  ($("$dir/.venv/bin/python" -V))"
}
build_venv /opt/weather-mcp  weathermcp
build_venv /opt/sql-explorer sqlxapp

# Assert the pin actually landed. Getting this backwards is silent until the
# first question, which then fails with an ImportError on RequestContext.
MCP_SRV_VER=$(/opt/weather-mcp/.venv/bin/python  -c 'import importlib.metadata as m; print(m.version("mcp"))')
MCP_APP_VER=$(/opt/sql-explorer/.venv/bin/python -c 'import importlib.metadata as m; print(m.version("mcp"))')
echo "   mcp: server=${MCP_SRV_VER}  app=${MCP_APP_VER}"
[[ ${MCP_SRV_VER%%.*} -ge 2 ]] || fail "server venv needs mcp>=2, has ${MCP_SRV_VER}"
[[ ${MCP_APP_VER%%.*} -lt 2 ]] || fail "app venv needs mcp<2, has ${MCP_APP_VER}"

# --- 4. Credentials ----------------------------------------------------------
# Created empty if absent and NEVER overwritten, so a re-run cannot clobber a
# working secret. Each is readable only by its own service user.
log "Credential files"
make_env() {
    local dir="$1" file="$2" owner="$3" template="$4"
    mkdir -p "$dir"; chmod 755 "$dir"
    if [[ -s "$file" ]]; then
        echo "   $file exists — left untouched"
    else
        printf '%s\n' "$template" > "$file"
        echo "   $file CREATED EMPTY — fill it in before the service will work"
    fi
    chown root:"$owner" "$file"
    chmod 640 "$file"
}
make_env /etc/weather-mcp  /etc/weather-mcp/mcp.env  weathermcp \
    "# Password for the mcp_ro Postgres role.
MCP_DB_PASSWORD="
make_env /etc/sql-explorer /etc/sql-explorer/app.env sqlxapp \
    "# Google AI Studio key for Gemini.
GOOGLE_API_KEY=
# Spend caps. Tighter here than the laptop defaults: this endpoint is public.
SQLX_MAX_SESSION_QUESTIONS=10
SQLX_MAX_DAILY_TOKENS=1500000"

# --- 5. systemd --------------------------------------------------------------
log "systemd units"
install -m 644 "$SRC/weather-mcp.service"  /etc/systemd/system/
install -m 644 "$SRC/sql-explorer.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet weather-mcp.service sql-explorer.service
echo "   installed and enabled"

# --- 6. nginx ----------------------------------------------------------------
log "nginx"
install -m 644 "$SRC/proxy_params_sqlx.inc" /etc/nginx/
if [[ ! -f /etc/nginx/conf.d/sqlx.conf ]]; then
    install -m 644 "$SRC/sqlx-http.conf" /etc/nginx/conf.d/sqlx.conf
    echo "   installed HTTP-only vhost (run enable-tls.sh after DNS)"
else
    echo "   /etc/nginx/conf.d/sqlx.conf exists — left as-is"
fi

# The weblog map entry. Without it this host falls to `default other`, and
# `other` on this box means "scanner hitting an unknown Host" — a real signal
# that must not be polluted by a legitimate vhost.
if grep -q "\"${HOSTNAME_APP}\"" /etc/nginx/conf.d/00-weblog.conf; then
    echo "   weblog map already has ${HOSTNAME_APP}"
else
    cp -a /etc/nginx/conf.d/00-weblog.conf "/etc/nginx/conf.d/00-weblog.conf.bak-$(date +%Y%m%d)"
    sed -i "s|^\(\s*\)\"pro\.dustincremascoli\.com\"\(\s*\)pro;|&\n\1\"${HOSTNAME_APP}\"\2${WEBLOG_SITE};|" \
        /etc/nginx/conf.d/00-weblog.conf
    grep -q "\"${HOSTNAME_APP}\"" /etc/nginx/conf.d/00-weblog.conf \
        || fail "could not add ${HOSTNAME_APP} to the weblog map — add it by hand"
    echo "   added ${HOSTNAME_APP} -> ${WEBLOG_SITE} to the weblog map"
fi

nginx -t || fail "nginx config invalid — nothing was reloaded, fix and re-run"

# RELOAD HERE, not after starting the services, and this ordering is a bug fix.
#
# The reload used to sit at the end of the script, after the "did the services
# start?" checks. When sql-explorer failed to start, `fail` exited before the
# reload -- leaving the vhost file and the weblog map edit ON DISK but NOT in the
# running config. The symptom is thoroughly misleading: requests for this host
# fall through to the default_server and get a 301 to HTTPS, and the injected
# beacon reports data-site="other", so it looks like the vhost and the map entry
# are both wrong when in fact neither had been loaded.
#
# The config is already proven valid by `nginx -t` above, so reloading now is
# safe. If the app is not up yet the vhost returns 502 for a few seconds, which
# is self-correcting and honest.
systemctl reload nginx
echo "   reloaded nginx"

# --- 7. Start ----------------------------------------------------------------
log "Starting services"
systemctl restart weather-mcp.service
for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q '127.0.0.1:8770' && break
    sleep 1
done
systemctl is-active --quiet weather-mcp.service \
    || fail "weather-mcp did not start: journalctl -u weather-mcp -n 40"

systemctl restart sql-explorer.service
for _ in $(seq 1 60); do
    curl -fsS http://127.0.0.1:8503/_stcore/health >/dev/null 2>&1 && break
    sleep 1
done
systemctl is-active --quiet sql-explorer.service \
    || fail "sql-explorer did not start: journalctl -u sql-explorer -n 40"

# --- 8. Assert the things that fail silently --------------------------------
log "Verification"

# Streamlit binding 0.0.0.0 would publish 8503 on the public EIP, past nginx and
# past its rate limits. This is the check that catches it.
if ss -ltn | awk '{print $4}' | grep -qE '(^|[^0-9.])0\.0\.0\.0:8503|^\*:8503|\[::\]:8503'; then
    fail "8503 is listening on a public address — --server.address=127.0.0.1 is missing"
fi
echo "   8503 is loopback-only"

if ss -ltn | awk '{print $4}' | grep -qE '(^|[^0-9.])0\.0\.0\.0:8770|^\*:8770|\[::\]:8770'; then
    fail "8770 is listening on a public address — MCP_HTTP_HOST is wrong"
fi
echo "   8770 is loopback-only"

curl -fsS http://127.0.0.1:8503/_stcore/health >/dev/null && echo "   streamlit health ok"

# End-to-end through the MCP protocol, not just a port check: a listening socket
# with a broken database credential looks identical from the outside.
#
# THE ENVIRONMENT IS LOAD-BEARING AND WAS MISSING. The connection settings live
# as `Environment=` lines in weather-mcp.service; /etc/weather-mcp/mcp.env holds
# only the password. Invoked without the unit's environment, db.py falls back to
# its development default of port 15432 -- the laptop's SSH tunnel -- so every
# check failed with "connection refused" on a box whose database was perfectly
# healthy, and the deploy reported a scary false negative every single time.
# The values are read back off the unit so there is one source of truth.
#
# Also: run the suite ONCE. This is 61 live database checks, and the previous
# version ran the whole thing twice -- once to test, once to print a summary it
# had already computed.
#
# `env $MCP_UNIT_ENV` is deliberately unquoted: systemctl returns space-separated
# KEY=VALUE pairs and they must word-split into separate arguments.
MCP_UNIT_ENV=$(systemctl show weather-mcp -p Environment --value)
SELFTEST_CMD="sudo -u weathermcp env \$(systemctl show weather-mcp -p Environment --value) \\
     bash -c 'set -a; . /etc/weather-mcp/mcp.env; set +a
       cd /opt/weather-mcp && ./.venv/bin/python -m weather_mcp.selftest'"
# shellcheck disable=SC2086
SELFTEST=$(sudo -u weathermcp env $MCP_UNIT_ENV bash -c \
    'set -a; . /etc/weather-mcp/mcp.env; set +a
     cd /opt/weather-mcp && ./.venv/bin/python -m weather_mcp.selftest' 2>&1 || true)

if grep -q "checks passed" <<<"$SELFTEST"; then
    tail -2 <<<"$SELFTEST" | sed 's/^/   /'
    # A summary line is printed even when individual checks failed, so say so
    # rather than letting "checks passed" read as all-clear.
    if grep -q "FAIL" <<<"$SELFTEST"; then
        echo "   ^ some checks FAILED. To see which:"
        echo "     $SELFTEST_CMD"
    fi
else
    echo "   selftest produced no summary — the suite could not run. Try:"
    echo "     $SELFTEST_CMD"
fi

log "Done"

# The remaining-steps list is CONDITIONAL. It used to print unconditionally, so
# every routine redeploy of an already-live site ended with "Nothing is publicly
# reachable until step 2 completes" and a list of first-install chores that were
# done months ago -- which reads as a failed deploy. Each step is now tested for.
REMAINING=0
note() { REMAINING=$((REMAINING + 1)); echo "  ${REMAINING}. $1"; }

echo "Both services are running and reachable on the loopback."
echo

if ! grep -q '[^[:space:]]' <<<"$(sed -n 's/^MCP_DB_PASSWORD=//p' /etc/weather-mcp/mcp.env)" \
   || ! grep -q '[^[:space:]]' <<<"$(sed -n 's/^GOOGLE_API_KEY=//p' /etc/sql-explorer/app.env)"; then
    note "Fill in the empty credential field(s):
       /etc/weather-mcp/mcp.env     MCP_DB_PASSWORD
       /etc/sql-explorer/app.env    GOOGLE_API_KEY
     then: systemctl restart weather-mcp sql-explorer"
fi

if ! getent hosts "${HOSTNAME_APP}" >/dev/null 2>&1; then
    note "Add the DNS A record at GoDaddy (manual — no Route53 zone, no API creds):
       ${HOSTNAME_APP}  A  <EC2_PUBLIC_IP>
     Confirm with: dig +short ${HOSTNAME_APP}"
fi

if [[ ! -d "/etc/letsencrypt/live/${HOSTNAME_APP}" ]]; then
    note "Issue the certificate: sudo bash ${SRC}/enable-tls.sh"
fi

if (( REMAINING == 0 )); then
    echo "Nothing outstanding: DNS resolves, the certificate is installed, and"
    echo "https://${HOSTNAME_APP}/ is serving this build."
else
    echo
    echo "Until the above is done, the site is not fully reachable."
fi

#!/usr/bin/env bash
# Local launcher. Checks the two things that are always the problem before
# starting Streamlit, because both fail in ways that look like app bugs:
# a dead SSH tunnel, and a missing MCP server.
set -euo pipefail
cd "$(dirname "$0")"

TUNNEL_PORT=15432
MCP_URL="${SQLX_MCP_URL:-http://127.0.0.1:8770/mcp}"
MCP_PORT="$(printf '%s' "$MCP_URL" | sed -E 's|.*://[^:]+:([0-9]+).*|\1|')"

if ! nc -z 127.0.0.1 "$TUNNEL_PORT" 2>/dev/null; then
    echo "The SSH tunnel on ${TUNNEL_PORT} is down. Public 5432 is closed, so it is"
    echo "the only route to Postgres. Bring it back with:"
    echo
    echo "  launchctl kickstart -k gui/\$(id -u)/com.dustincremascoli.pgtunnel"
    echo
    exit 1
fi

if ! nc -z 127.0.0.1 "$MCP_PORT" 2>/dev/null; then
    echo "Nothing is listening on ${MCP_PORT}, so the MCP server is not running."
    echo "Tool discovery would return an empty list and the model would answer"
    echo "from nothing. Start it in another shell:"
    echo
    echo "  cd ../mcp_server && ./.venv/bin/python -m weather_mcp.server --http"
    echo
    exit 1
fi

exec ./.venv/bin/streamlit run app.py \
    --server.address=127.0.0.1 \
    --server.port="${SQLX_PORT:-8503}" \
    --server.headless=true \
    --browser.gatherUsageStats=false

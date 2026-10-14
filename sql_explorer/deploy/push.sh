#!/usr/bin/env bash
# Ship sql_explorer/ and mcp_server/ to the EC2 box and provision them. Run FROM THE MAC:
#
#     bash sql_explorer/deploy/push.sh        # from weather-sql-explorer/
#
# The same steps README-deploy.md §Deploying a change lists, as one command: stage the
# code, compile it against the box's interpreter, then run provision.sh. The compile
# check is what keeps a syntax error from becoming a crash-loop on a public page.
#
# Safe to re-run. provision.sh is idempotent and never overwrites a credential file.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"      # weather-sql-explorer/
SSH_HOST="${SSH_HOST:-awsvm}"          # see ~/.ssh/config
STAGE=/tmp/sqlx-deploy
UNITS=(sql-explorer.service weather-mcp.service)

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ -d "$ROOT/sql_explorer" && -d "$ROOT/mcp_server" ]] \
    || fail "expected sql_explorer/ and mcp_server/ under $ROOT"

log "Checking the box is reachable"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" true \
    || fail "cannot ssh to ${SSH_HOST}. Check ~/.ssh/config."
echo "   ${SSH_HOST} ok"

log "Diffing the live unit files against the repo"
# ec2-nginx/CLAUDE.md: the box has been ahead of the repo before, and a blind redeploy
# regresses it. Shown, not enforced -- a deploy that changes a unit always differs.
for unit in "${UNITS[@]}"; do
    if ssh "$SSH_HOST" "cat /etc/systemd/system/${unit}" \
            | diff -u --label "box:${unit}" --label "repo:${unit}" - \
                   "$ROOT/sql_explorer/deploy/${unit}"; then
        echo "   ${unit} identical"
    fi
done

log "Staging the code"
rsync -az --delete \
      --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
      --exclude '.budget.json' --exclude '.git/' \
      "$ROOT/mcp_server" "$ROOT/sql_explorer" "${SSH_HOST}:${STAGE}/"
echo "   staged at ${STAGE}"

log "Compiling against the box's interpreter"
ssh "$SSH_HOST" "cd ${STAGE}/sql_explorer && /usr/bin/python3.11 -m py_compile *.py" \
    || fail "the staged code does not compile on the box; nothing was restarted"
echo "   ok"

log "Provisioning (sudo on the box)"
ssh -t "$SSH_HOST" "sudo bash ${STAGE}/sql_explorer/deploy/provision.sh"

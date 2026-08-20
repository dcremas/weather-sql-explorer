#!/usr/bin/env bash
# Obtain a Let's Encrypt certificate and swap in the TLS vhost.
#
# Run ON THE BOX, AFTER the DNS A record resolves:
#   sudo bash /tmp/sqlx-deploy/deploy/enable-tls.sh
#
# SELF-REVERTING. If `nginx -t` fails with the TLS vhost in place, the previous
# HTTP-only file is restored and nginx is left running on the working config —
# a broken vhost file takes down EVERY site on the box, not just this one, so
# this must never leave nginx unable to load.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
HOSTNAME_APP="${HOSTNAME_APP:-sql.dustincremascoli.com}"
EMAIL="${CERTBOT_EMAIL:-dustin.cremascoli@syw.com}"
VHOST=/etc/nginx/conf.d/sqlx.conf

log() { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo"

log "Checking DNS"
RESOLVED=$(dig +short "$HOSTNAME_APP" | tail -1)
echo "   ${HOSTNAME_APP} -> ${RESOLVED:-<nothing>}"
[[ -n "$RESOLVED" ]] || fail "${HOSTNAME_APP} does not resolve.
Add the A record at GoDaddy first:  ${HOSTNAME_APP}  A  <EC2_PUBLIC_IP>
certbot cannot validate a host that does not resolve, and failed attempts count
against Let's Encrypt rate limits."

# IMDSv2. This box requires a token (the hardening work enforced it), so the
# plain IMDSv1 GET returns 401 -- which, because MY_IP is only checked when
# non-empty, made this whole guard silently dead code. Fetch the token first.
IMDS_TOKEN=$(curl -fsS --max-time 5 -X PUT \
    "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || echo "")
if [[ -n "$IMDS_TOKEN" ]]; then
    MY_IP=$(curl -fsS --max-time 5 \
        -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
        http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "")
else
    MY_IP=""
fi
if [[ -z "$MY_IP" ]]; then
    # Say so rather than passing quietly: a skipped check that looks like a
    # passed check is how the wrong A record reaches certbot.
    echo "   WARNING: could not read this instance's public IP from IMDS —"
    echo "   skipping the does-DNS-point-here check. Verify by hand if unsure."
else
    echo "   this instance: ${MY_IP}"
fi
if [[ -n "$MY_IP" && "$RESOLVED" != "$MY_IP" ]]; then
    fail "${HOSTNAME_APP} resolves to ${RESOLVED} but this box is ${MY_IP}.
Fix the A record, or wait for the old value's TTL to expire."
fi

log "Requesting the certificate"
mkdir -p /var/www/letsencrypt
if [[ -d "/etc/letsencrypt/live/${HOSTNAME_APP}" ]]; then
    echo "   certificate already exists — skipping issuance"
else
    # --webroot, not --nginx: the nginx plugin rewrites the vhost itself, which
    # would fight the checked-in file. The HTTP vhost installed by provision.sh
    # already serves /.well-known/acme-challenge/ from this root.
    certbot certonly --webroot -w /var/www/letsencrypt \
        -d "$HOSTNAME_APP" \
        --non-interactive --agree-tos -m "$EMAIL" \
        || fail "certbot failed — nothing was changed"
fi

log "Installing the TLS vhost"
BACKUP="${VHOST}.bak-$(date +%Y%m%d-%H%M%S)"
cp -a "$VHOST" "$BACKUP"
install -m 644 "$SRC/sqlx.conf" "$VHOST"

if ! nginx -t; then
    cp -a "$BACKUP" "$VHOST"
    nginx -t || fail "config is broken even after reverting — DO NOT reload nginx"
    fail "TLS vhost failed nginx -t; reverted to ${BACKUP}. Nothing was reloaded."
fi

systemctl reload nginx
echo "   reloaded (previous file kept at ${BACKUP})"

log "Verification"
systemctl is-active --quiet certbot-renew.timer \
    && echo "   certbot-renew.timer active" \
    || echo "   WARNING: certbot-renew.timer is NOT active — this cert will expire"

code=$(curl -s -o /dev/null -w '%{http_code}' -L "https://${HOSTNAME_APP}/")
echo "   https://${HOSTNAME_APP}/ -> ${code}"
[[ "$code" == "200" ]] || echo "   (expected 200 — check journalctl -u sql-explorer)"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://${HOSTNAME_APP}/")
echo "   http redirect -> ${code} (expect 301)"

echo
echo "The WebSocket cannot be tested with curl — it reports 200 on a WS probe"
echo "because it is not a real WebSocket client. Use a raw socket, or just open"
echo "the page and ask a question: if the socket is broken the page reconnect-loops"
echo "and no answer ever arrives."

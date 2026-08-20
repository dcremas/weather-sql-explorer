# Shared proxy settings for the Streamlit SQL-explorer upstream.
#
# Same shape as proxy_params_analytics.inc and for the same reason: Streamlit is
# not a request/response app. The browser opens ONE WebSocket to /_stcore/stream
# and every UI update travels over it, so this needs the Upgrade handshake and a
# read timeout in hours. With the 30s timeout the Flask vhosts use, the socket is
# torn down every 30 seconds and the page reconnect-loops.
proxy_pass http://sqlx_app;
proxy_http_version 1.1;

proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;

# $connection_upgrade comes from the map in sqlx.conf: "upgrade" for a WebSocket
# request, "close" otherwise. Hard-coding "upgrade" breaks plain HTTP through
# keepalive.
proxy_set_header Upgrade           $http_upgrade;
proxy_set_header Connection        $connection_upgrade;

proxy_redirect off;
proxy_buffering off;
proxy_connect_timeout 5s;
proxy_send_timeout    3600s;
# A question can legitimately take a while: the agent may run several queries,
# each capped at 30s server-side, under a 180s per-turn ceiling in the app.
proxy_read_timeout    3600s;

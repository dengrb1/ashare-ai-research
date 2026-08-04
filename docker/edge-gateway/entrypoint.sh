#!/bin/sh
set -eu

: "${EDGE_DOMAIN:?EDGE_DOMAIN must be set to the public DNS name}"
: "${EDGE_ACME_EMAIL:?EDGE_ACME_EMAIL must be set for ACME account recovery}"

ACME_HOME=/var/lib/acme
ACME_WEBROOT=/var/lib/acme-webroot
CERT_DIR=/etc/edge/certs
ACME_CA_SERVER="${EDGE_ACME_CA_SERVER:-letsencrypt}"
FRPC_PID=""
RENEW_PID=""
NGINX_PID=""

mkdir -p "$ACME_HOME" "$ACME_WEBROOT" "$CERT_DIR" /tmp/client_temp /tmp/proxy_temp \
  /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp
# The container intentionally drops DAC_OVERRIDE.  Keep ACME's persistent
# account and certificate stores root-owned so the root-run acme.sh process can
# create and renew keys; only Nginx's worker temp paths need nginx ownership.
chown -R nginx:nginx /tmp/client_temp /tmp/proxy_temp /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp

export EDGE_DOMAIN
envsubst '${EDGE_DOMAIN}' < /etc/nginx/templates/edge.conf.template > /tmp/edge.conf

stop_children() {
  [ -n "$NGINX_PID" ] && nginx -s quit >/dev/null 2>&1 || true
  [ -n "$RENEW_PID" ] && kill "$RENEW_PID" >/dev/null 2>&1 || true
  [ -n "$FRPC_PID" ] && kill "$FRPC_PID" >/dev/null 2>&1 || true
  wait ${NGINX_PID:-} ${RENEW_PID:-} ${FRPC_PID:-} 2>/dev/null || true
}
trap 'stop_children; exit 0' INT TERM

if [ "${EDGE_FRPC_ENABLED:-false}" = "true" ]; then
  if [ ! -s /etc/edge/frpc.toml ]; then
    echo "EDGE_FRPC_ENABLED=true requires a non-empty /etc/edge/frpc.toml mount" >&2
    exit 1
  fi
  frpc -c /etc/edge/frpc.toml &
  FRPC_PID=$!
fi

if [ ! -s "$CERT_DIR/fullchain.pem" ] || [ ! -s "$CERT_DIR/key.pem" ]; then
  acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --server "$ACME_CA_SERVER" \
    --register-account -m "$EDGE_ACME_EMAIL"
  # Standalone cert issuance may fail transiently under low resource limits
  # (e.g. Docker pids_limit).  Retry twice with a short back-off, then fall
  # through to a self-signed pair so nginx can still start.
  ISSUE_OK=0
  for attempt in 1 2 3; do
    if acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --server "$ACME_CA_SERVER" \
         --issue --standalone -d "$EDGE_DOMAIN" --keylength ec-256; then
      ISSUE_OK=1
      break
    fi
    echo "ACME issue attempt $attempt failed; retrying in 5s..." >&2
    sleep 5
  done
  if [ "$ISSUE_OK" -eq 1 ]; then
    acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --install-cert -d "$EDGE_DOMAIN" \
      --ecc --key-file "$CERT_DIR/key.pem" --fullchain-file "$CERT_DIR/fullchain.pem"
  else
    echo "WARNING: ACME issuance failed; falling back to self-signed certificate for $EDGE_DOMAIN" >&2
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout "$CERT_DIR/key.pem" \
      -out "$CERT_DIR/fullchain.pem" -days 30 -nodes \
      -subj "/CN=$EDGE_DOMAIN" -addext "subjectAltName=DNS:$EDGE_DOMAIN"
  fi
fi

nginx -t
nginx -g 'daemon off;' &
NGINX_PID=$!

(
  while :; do
    sleep 12h
    acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --cron --server "$ACME_CA_SERVER"
    nginx -s reload
  done
) &
RENEW_PID=$!

wait "$NGINX_PID" 2>/dev/null || true
# wait(1) returns 127 when the pid is not a child of this shell
# (e.g. nginx exited before we reached wait).  Treat that as a
# normal exit so Docker doesn't show a confusing "Exited (127)".
stop_children
exit 0

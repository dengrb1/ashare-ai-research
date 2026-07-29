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
  acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --server "$ACME_CA_SERVER" \
    --issue --standalone -d "$EDGE_DOMAIN" --keylength ec-256
  acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --install-cert -d "$EDGE_DOMAIN" \
    --ecc --key-file "$CERT_DIR/key.pem" --fullchain-file "$CERT_DIR/fullchain.pem"
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

wait "$NGINX_PID"
EXIT_CODE=$?
stop_children
exit "$EXIT_CODE"

#!/bin/sh
set -eu

: "${EDGE_DOMAIN:?EDGE_DOMAIN must be set to the public DNS name}"
: "${EDGE_ACME_EMAIL:?EDGE_ACME_EMAIL must be set for ACME account recovery}"

ACME_HOME=/var/lib/acme
ACME_WEBROOT=/var/lib/acme-webroot
CERT_DIR=/etc/edge/certs
BOOTSTRAP_MARKER="$CERT_DIR/.acme-bootstrap"
ACME_CA_SERVER="${EDGE_ACME_CA_SERVER:-letsencrypt}"
LOG_DIR="${EDGE_GATEWAY_LOG_DIR:-/var/log/edge}"
FRPC_PID=""
RENEW_PID=""
NGINX_PID=""

mkdir -p "$ACME_HOME" "$ACME_WEBROOT" "$CERT_DIR" /tmp/client_temp /tmp/proxy_temp \
  /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp

# Compose mounts a persistent named volume at /var/log/edge.  A container
# created from an older Compose definition may not have that mount; keep the
# immutable-root-filesystem contract and use tmpfs logs until it is recreated.
if ! mkdir -p "$LOG_DIR" 2>/dev/null || [ ! -w "$LOG_DIR" ]; then
  echo "edge-gateway log directory is unavailable; using /tmp/edge logs until the container is recreated" >&2
  LOG_DIR=/tmp/edge
  mkdir -p "$LOG_DIR"
fi
FRPC_LOG="$LOG_DIR/frpc.log"

# The container intentionally drops DAC_OVERRIDE.  Keep ACME's persistent
# account and certificate stores root-owned so the root-run acme.sh process can
# create and renew keys; only Nginx's worker temp paths need nginx ownership.
chown -R nginx:nginx /tmp/client_temp /tmp/proxy_temp /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp

export EDGE_DOMAIN
envsubst '${EDGE_DOMAIN}' < /etc/nginx/templates/edge.conf.template > /tmp/edge.conf
if [ -s /etc/edge/managed.conf ]; then
  cat /etc/edge/managed.conf >> /tmp/edge.conf
fi

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
  : > "$FRPC_LOG"
  frpc -c /etc/edge/frpc.toml >> "$FRPC_LOG" 2>&1 &
  FRPC_PID=$!
fi

certificate_is_self_signed() {
  [ -s "$CERT_DIR/fullchain.pem" ] || return 1
  subject=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject 2>/dev/null || true)
  issuer=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -issuer 2>/dev/null || true)
  [ -n "$subject" ] && [ "${subject#subject=}" = "${issuer#issuer=}" ]
}

needs_acme_certificate() {
  [ -f "$BOOTSTRAP_MARKER" ] || [ ! -s "$CERT_DIR/fullchain.pem" ] || \
    [ ! -s "$CERT_DIR/key.pem" ] || certificate_is_self_signed
}

prepare_bootstrap_certificate() {
  if needs_acme_certificate; then
    : > "$BOOTSTRAP_MARKER"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
      -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/fullchain.pem" -days 7 -nodes \
      -subj "/CN=$EDGE_DOMAIN" -addext "subjectAltName=DNS:$EDGE_DOMAIN"
  fi
}

migrate_acme_webroot() {
  # Older releases issued in standalone mode.  Migrate their persisted
  # renewal record so future renewals do not try to bind Nginx's port 80.
  domain_conf="$ACME_HOME/${EDGE_DOMAIN}_ecc/${EDGE_DOMAIN}.conf"
  if [ -f "$domain_conf" ] && grep -q "^Le_Webroot='no'" "$domain_conf"; then
    sed -i "s|^Le_Webroot='no'.*$|Le_Webroot='$ACME_WEBROOT'|" "$domain_conf"
    echo "Migrated ACME renewal for $EDGE_DOMAIN to webroot mode" >&2
  fi
}

issue_certificate() {
  acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --server "$ACME_CA_SERVER" \
    --register-account -m "$EDGE_ACME_EMAIL" >/dev/null 2>&1 || true
  for attempt in 1 2 3; do
    # Webroot mode keeps Nginx online, which is required both for FRP users
    # and for renewals after the initial certificate has been installed.
    acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --server "$ACME_CA_SERVER" \
      --issue --webroot "$ACME_WEBROOT" -d "$EDGE_DOMAIN" --keylength ec-256 || true
    if acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --install-cert \
      -d "$EDGE_DOMAIN" --ecc --key-file "$CERT_DIR/key.pem" \
      --fullchain-file "$CERT_DIR/fullchain.pem"; then
      if ! certificate_is_self_signed; then
        rm -f "$BOOTSTRAP_MARKER"
        nginx -s reload >/dev/null 2>&1 || true
        echo "ACME certificate installed for $EDGE_DOMAIN" >&2
        return 0
      fi
    fi
    echo "ACME issue attempt $attempt failed; retrying in 5s..." >&2
    sleep 5
  done
  echo "WARNING: ACME issuance failed; serving the temporary certificate for $EDGE_DOMAIN" >&2
  return 1
}

migrate_acme_webroot
prepare_bootstrap_certificate

nginx -t
nginx -g 'daemon off;' &
NGINX_PID=$!

if needs_acme_certificate; then
  issue_certificate || true
fi

(
  while :; do
    if needs_acme_certificate; then
      sleep 1h
      issue_certificate || true
    else
      sleep 12h
      if acme.sh --home "$ACME_HOME" --config-home "$ACME_HOME" --cron --server "$ACME_CA_SERVER"; then
        nginx -s reload
      else
        echo "WARNING: ACME renewal failed; retaining the current certificate" >&2
      fi
    fi
  done
) &
RENEW_PID=$!

wait "$NGINX_PID" 2>/dev/null || true
# wait(1) returns 127 when the pid is not a child of this shell
# (e.g. nginx exited before we reached wait).  Treat that as a
# normal exit so Docker doesn't show a confusing "Exited (127)".
stop_children
exit 0

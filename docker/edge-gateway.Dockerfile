FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

ARG FRP_VERSION=0.68.0
ARG FRP_SHA256=3cf934477f4fb1ee9e19e49c31fb33f5ffe3283300076f59afad8b8ccf1e1621
ARG ACME_SH_VERSION=3.1.1
ARG ACME_SH_SHA256=c5d623ac0af400e83cd676aefaf045228f60e9fc597fea5db4c3a5bd7f6bfcf4
ARG EDGE_GATEWAY_VERSION=2.0.4-alpha.1

LABEL org.opencontainers.image.title="Ashare AI Edge Gateway" \
      org.opencontainers.image.version="${EDGE_GATEWAY_VERSION}" \
      org.opencontainers.image.description="Optional HTTPS edge gateway (alpha)"

RUN apk add --no-cache ca-certificates curl gettext openssl socat \
    && curl --fail --location --silent --show-error \
      --output /tmp/frp.tar.gz \
      "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz" \
    && echo "${FRP_SHA256}  /tmp/frp.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/frp.tar.gz -C /tmp \
    && install -m 0755 "/tmp/frp_${FRP_VERSION}_linux_amd64/frpc" /usr/local/bin/frpc \
    && curl --fail --location --silent --show-error \
      --output /tmp/acme.sh.tar.gz \
      "https://github.com/acmesh-official/acme.sh/archive/refs/tags/${ACME_SH_VERSION}.tar.gz" \
    && echo "${ACME_SH_SHA256}  /tmp/acme.sh.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/acme.sh.tar.gz -C /tmp \
    && install -m 0755 "/tmp/acme.sh-${ACME_SH_VERSION}/acme.sh" /usr/local/bin/acme.sh \
    && rm -rf /tmp/frp.tar.gz /tmp/acme.sh.tar.gz \
      "/tmp/frp_${FRP_VERSION}_linux_amd64" "/tmp/acme.sh-${ACME_SH_VERSION}"

COPY docker/edge-gateway/nginx.conf /etc/nginx/nginx.conf
COPY docker/edge-gateway/edge.conf.template /etc/nginx/templates/edge.conf.template
COPY docker/edge-gateway/entrypoint.sh /usr/local/bin/edge-gateway-entrypoint

# ACME and certificate directories are copied into named volumes on first use.
# They must remain root-owned because the runtime drops CAP_DAC_OVERRIDE while
# acme.sh still runs as root.
RUN chmod 0755 /usr/local/bin/edge-gateway-entrypoint \
    && sed -i 's/\r$//' /usr/local/bin/edge-gateway-entrypoint \
    && mkdir -p /var/lib/acme /var/lib/acme-webroot /etc/edge/certs /var/cache/nginx \
    && chown -R nginx:nginx /var/cache/nginx

EXPOSE 80 443
ENTRYPOINT ["/usr/local/bin/edge-gateway-entrypoint"]

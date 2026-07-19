FROM postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777 AS hardened

# The upstream entrypoint only needs privilege dropping from gosu. su-exec is
# smaller and avoids shipping gosu's vulnerable, otherwise-unused Go runtime.
RUN apk upgrade --no-cache \
    && apk add --no-cache su-exec \
    && sed -i 's/exec gosu postgres/exec su-exec postgres/' /usr/local/bin/docker-entrypoint.sh \
    && grep -q 'exec su-exec postgres' /usr/local/bin/docker-entrypoint.sh \
    && rm -f /usr/local/bin/gosu

# Flatten the merged filesystem so the removed gosu binary is not retained in
# an inaccessible lower image layer or reported by registry scanners.
FROM scratch
COPY --from=hardened / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=en_US.utf8 \
    PG_MAJOR=16 \
    PG_VERSION=16.14 \
    PGDATA=/var/lib/postgresql/data
VOLUME ["/var/lib/postgresql/data"]
ENTRYPOINT ["docker-entrypoint.sh"]
STOPSIGNAL SIGINT
EXPOSE 5432
CMD ["postgres"]

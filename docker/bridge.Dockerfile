# Multi-target Dockerfile for Python bridge services (stdlib only)
FROM python:3.12-alpine AS base

WORKDIR /app

# Install runtime dependencies
RUN apk add --no-cache ca-certificates tzdata && \
    addgroup -g 10002 bridge && \
    adduser -D -u 10002 -G bridge bridge

# Quote bridge target
FROM base AS quote-bridge

COPY tools/quote_bridge/server.py /app/

USER bridge

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3)" || exit 1

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8081"]

# Multi-stage Dockerfile for ashare-model-gateway (Rust)
# Stage 1: Build the Rust binary
FROM rust:1.83-alpine AS builder

WORKDIR /build

# Install build dependencies
RUN apk add --no-cache musl-dev

# Copy Cargo manifest and lock file first for layer caching
COPY gateway/Cargo.toml gateway/Cargo.lock ./

# Create a dummy main.rs to build dependencies separately
RUN mkdir src && \
    echo "fn main() {}" > src/main.rs && \
    cargo build --release && \
    rm -rf src

# Copy actual source code
COPY gateway/src ./src/

# Build the real binary (dependencies are cached)
RUN cargo build --release && \
    strip target/release/ashare-model-gateway

# Stage 2: Runtime image
FROM alpine:3.21

# Install runtime dependencies
RUN apk add --no-cache ca-certificates tzdata && \
    addgroup -g 10001 gateway && \
    adduser -D -u 10001 -G gateway gateway

# Copy the binary from builder
COPY --from=builder /build/target/release/ashare-model-gateway /usr/local/bin/

# Run as non-root user
USER gateway

EXPOSE 8787

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8787/health/ready || exit 1

CMD ["ashare-model-gateway"]

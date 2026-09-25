FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser
COPY pyproject.toml README.md LICENSE requirements.lock requirements.runtime.lock /app/
RUN pip install --no-cache-dir --upgrade "pip==26.1.2" "setuptools==83.0.0" \
    && pip install --no-cache-dir --requirement requirements.runtime.lock
COPY src /app/src
COPY migrations /app/migrations
COPY configs /app/configs
COPY alembic.ini /app/alembic.ini
RUN pip install --no-deps .
ENV MALLOC_ARENA_MAX=2 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    ARROW_IO_THREADS=1
# The immutable runtime never invokes Debian package tooling. Removing the
# otherwise-unused essential perl-base package eliminates its unfixed runtime
# CVEs without affecting Python, health checks, migrations, or workers.
RUN dpkg --purge --force-remove-essential perl-base \
    && mkdir -p /data/lake /data/objects /data/private \
    && chown -R appuser:appuser /app /data
USER appuser

CMD ["ashare-ai", "api"]

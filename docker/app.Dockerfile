FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
ARG NEODATA_FINANCIAL_SEARCH_COMMIT=369fd3961d3a1482005e9673a5fc635a7595e710
ARG NEODATA_FINANCIAL_SEARCH_SHA256=733744cebc45345351c1bed4ca476bda631cff098ab44b47a5cb8d10a12b4009
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser
COPY pyproject.toml README.md LICENSE requirements.lock requirements.runtime.lock /app/
RUN pip install --no-cache-dir --upgrade "pip==26.1.2" "setuptools==83.0.0" \
    && pip install --no-cache-dir --requirement requirements.runtime.lock
RUN python -c "import hashlib,pathlib,urllib.request; url='https://raw.githubusercontent.com/Garyjie/neodata-financial-search/${NEODATA_FINANCIAL_SEARCH_COMMIT}/query.py'; data=urllib.request.urlopen(url, timeout=30).read(); actual=hashlib.sha256(data).hexdigest(); expected='${NEODATA_FINANCIAL_SEARCH_SHA256}'; assert actual == expected, (actual, expected); target=pathlib.Path('/opt/neodata-financial-search/query.py'); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)"
COPY src /app/src
COPY migrations /app/migrations
COPY configs /app/configs
COPY alembic.ini /app/alembic.ini
RUN pip install --no-deps .
ENV NEODATA_FINANCIAL_SEARCH_PATH=/opt/neodata-financial-search/query.py \
    MALLOC_ARENA_MAX=2 \
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

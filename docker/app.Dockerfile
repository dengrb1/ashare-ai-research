FROM python:3.11.9-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
ARG NEODATA_FINANCIAL_SEARCH_COMMIT=369fd3961d3a1482005e9673a5fc635a7595e710
ARG NEODATA_FINANCIAL_SEARCH_SHA256=733744cebc45345351c1bed4ca476bda631cff098ab44b47a5cb8d10a12b4009
RUN useradd --create-home --uid 10001 appuser
COPY pyproject.toml README.md requirements.lock /app/
RUN pip install --requirement requirements.lock
RUN python -c "import hashlib,pathlib,urllib.request; url='https://raw.githubusercontent.com/Garyjie/neodata-financial-search/${NEODATA_FINANCIAL_SEARCH_COMMIT}/query.py'; data=urllib.request.urlopen(url, timeout=30).read(); actual=hashlib.sha256(data).hexdigest(); expected='${NEODATA_FINANCIAL_SEARCH_SHA256}'; assert actual == expected, (actual, expected); target=pathlib.Path('/opt/neodata-financial-search/query.py'); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)"
COPY src /app/src
COPY migrations /app/migrations
COPY configs /app/configs
COPY alembic.ini /app/alembic.ini
RUN pip install --no-deps .
ENV NEODATA_FINANCIAL_SEARCH_PATH=/opt/neodata-financial-search/query.py
RUN mkdir -p /data/lake /data/objects && chown -R appuser:appuser /app /data
USER appuser

CMD ["ashare-ai", "api"]

FROM python:3.14-slim AS builder

WORKDIR /app
COPY pyproject.toml config.example.yaml ./
COPY src/ src/
# The stylesheet: the Tailwind standalone CLI (one binary, pinned and checked
# by sha256 in the script) compiles static/src/app.css. Built here so neither
# the CLI nor curl reaches the final image, and before `pip install` so the
# wheel carries the result.
COPY scripts/build-css.sh scripts/build-css.sh
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && TAILWIND_CACHE=/tmp/tailwind sh scripts/build-css.sh \
    && rm -rf /tmp/tailwind
RUN pip install --no-cache-dir .

FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/src /app/src
COPY alembic.ini /app/alembic.ini
COPY config.example.yaml /app/config.example.yaml
COPY alembic/ /app/alembic/

WORKDIR /app
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/data/config.yaml

VOLUME ["/data/sessions", "/data/torrents"]
EXPOSE 4850

CMD ["python", "-m", "vettarr"]

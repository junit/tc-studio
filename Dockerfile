# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS web-builder

WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable \
    && corepack prepare pnpm@12.15.0 --activate \
    && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build


FROM python:3.13-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py ./
COPY apis/ ./apis/
COPY core/ ./core/
COPY scripts/ ./scripts/
COPY static/ ./static/
COPY --from=web-builder /static/ ./static/

RUN groupadd --system --gid 10001 tcstudio \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \
       --shell /usr/sbin/nologin tcstudio \
    && mkdir -p /var/lib/tc-studio/data /var/lib/tc-studio/storage \
    && chown -R tcstudio:tcstudio /app /var/lib/tc-studio

ENV TC_STUDIO_DATABASE_URL=sqlite:////var/lib/tc-studio/tc_studio.sqlite3 \
    TC_STUDIO_DATA_DIR=/var/lib/tc-studio/data \
    TC_STUDIO_STORAGE_DIR=/var/lib/tc-studio/storage \
    TC_STUDIO_RESULTS_DIR=/var/lib/tc-studio/storage/batch_results \
    TC_STUDIO_HISTORY_PATH=/var/lib/tc-studio/storage/history.json

VOLUME ["/var/lib/tc-studio"]

USER tcstudio

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# FlightLens: one container with the web UI, API and analysis engine.
#   docker compose up -d --build   →   http://localhost:8000

# --- 1. Build the web UI -------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- 2. Python runtime -----------------------------------------------------------
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist ./web/dist

# Run as an unprivileged user; flights live in the /data volume.
RUN useradd --system --uid 10001 --no-create-home flightlens \
    && mkdir /data && chown flightlens /data
USER flightlens
ENV FLIGHTLENS_DATA=/data \
    FLIGHTLENS_WEB=/app/web/dist \
    FLIGHTLENS_LOG_FORMAT=json \
    PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1
CMD ["flightlens", "serve", "--host", "0.0.0.0", "--port", "8000"]

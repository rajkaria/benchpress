# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS console
WORKDIR /src/packages/console
COPY packages/console/package.json packages/console/package-lock.json ./
RUN npm ci
COPY packages/console/ ./
RUN mkdir -p /src/src/benchpress && npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BENCHPRESS_HOST=0.0.0.0 BENCHPRESS_PORT=8787 \
    BENCHPRESS_STORE=sqlite:////data/benchpress.db
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY docs/PYPI.md docs/PYPI.md
COPY src/ src/
COPY --from=console /src/src/benchpress/console_dist/ src/benchpress/console_dist/
RUN uv pip install --system --no-cache ".[server,postgres]" \
    && useradd --uid 10001 --create-home benchpress \
    && mkdir -p /data && chown benchpress /data
USER 10001
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status == 200 else 1)"
ENTRYPOINT ["benchpress", "serve"]

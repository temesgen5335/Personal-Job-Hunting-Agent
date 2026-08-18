# Backend image: FastAPI orchestrator, the Telegram bot, and the CLI scripts.
# The dashboard is a separate image (dashboard/Dockerfile) because it is a Node SSR
# server — one image carrying both runtimes would be larger and rebuild on every
# change to either half.
#
# Playwright is NOT installed by default. It pulls ~400 MB of browser and is only
# needed for Tier-2 ATS form-fill, which is optional:
#     docker build --build-arg WITH_BROWSER=1 .

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first: source changes must not invalidate the pip cache.
COPY pyproject.toml README.md ./
COPY src/jobagent/__init__.py src/jobagent/__init__.py
RUN pip install --no-cache-dir -e ".[api,llm,telegram]"

COPY src/ src/
COPY scripts/ scripts/
COPY config/preferences.example.toml config/

ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      pip install --no-cache-dir playwright && playwright install --with-deps chromium; \
    fi

# The store, the profile overlay and the CV all live here. Mount it as a volume —
# without one, every container restart loses the entire job history.
VOLUME ["/app/data"]
ENV JOBAGENT_DB_PATH=/app/data/jobagent.db

# 0.0.0.0 inside a container is the container's own interface, not the host's — the
# port is only reachable via an explicit `ports:` mapping. This is NOT the same as
# setting HOST=0.0.0.0 on a bare VPS, which exposes unauthenticated reads (SECURITY.md).
ENV HOST=0.0.0.0 PORT=8077
EXPOSE 8077

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8077/health', timeout=4).status==200 else 1)"

CMD ["python", "scripts/run_api.py"]

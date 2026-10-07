# ShopPilot API image (blueprint Step Y). Secrets are never baked in: they arrive as environment variables at start.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed

RUN pip install --no-cache-dir uv

WORKDIR /app

# 1) dependencies first: this layer is cached until pyproject.toml or uv.lock change
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) the application code
COPY src ./src
COPY configs ./configs
COPY alembic.ini ./
COPY scripts ./scripts
COPY data/seed ./data/seed
RUN uv sync --frozen --no-dev

# 3) run as a normal user, not root. The app writes logs, reports and the embedding model cache.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/logs /app/reports /app/.cache/fastembed \
    && chown -R app:app /app/logs /app/reports /app/.cache
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "shoppilot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

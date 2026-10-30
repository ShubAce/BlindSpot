# BlindSpot API — container for Hugging Face Spaces (Docker SDK) or any container host.
# Installs the exact locked dependencies with uv, runs as uid 1000 (a Spaces requirement)
# and serves FastAPI on $PORT (7860 on Spaces).
FROM python:3.12-slim

# libgomp1: OpenMP runtime needed by the LightGBM wheel; ca-certificates for TLS to upstream APIs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.20 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    CACHE_DIR=/tmp/blindspot-cache \
    PORT=7860

RUN useradd -m -u 1000 app
WORKDIR /app

# Dependencies first so code changes do not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY blindspot ./blindspot
RUN mkdir -p /tmp/blindspot-cache && chown -R app:app /app /tmp/blindspot-cache

USER app
EXPOSE 7860

# --proxy-headers: Spaces terminates TLS in front of the container.
CMD ["sh", "-c", "uvicorn blindspot.api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]

# a2a-hub server image. Self-contained: token auth is in-process, no proxy needed.
#   docker build -t a2a-hub .
#   docker run -p 8000:8000 -e A2A_HUB_TOKENS="tok:agent" a2a-hub
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv from its official image (pinned by tag for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

WORKDIR /app

# Dependency layer (cacheable): manifests first. Every file referenced by the project
# metadata must be here, or building the local package fails inside the image:
# `readme` needs README.md and `license-files` needs LICENSE.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Persistent SQLite mailbox outside the image layer.
ENV A2A_HUB_DB_PATH=/data/a2a-hub.db
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Run as a non-root user.
RUN useradd --system --uid 10001 hub && chown -R hub /app /data
USER hub

# Run the console script installed in the venv (no uv at runtime: compatible with
# readOnlyRootFilesystem).
CMD ["/app/.venv/bin/a2a-hub"]

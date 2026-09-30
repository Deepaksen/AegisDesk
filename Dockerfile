# AegisDesk application image (Milestone 10): API, MCP servers, migrations and UI.
#
#   docker build -t aegisdesk .                         # API / MCP / CLI
#   docker build -t aegisdesk-ui --build-arg GROUPS="--group ui" .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1

WORKDIR /app
ARG GROUPS=""
# Dependencies first (cached layer), exactly as locked; no dev tools in the image.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-default-groups ${GROUPS}

COPY src ./src
COPY apps ./apps
COPY config ./config
COPY prompts ./prompts
COPY data ./data
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-default-groups ${GROUPS}

RUN useradd --create-home --uid 10001 aegis && mkdir -p /app/.aegisdesk && chown aegis /app/.aegisdesk
USER aegis
EXPOSE 8000 8765 8501
CMD ["aegisdesk", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]

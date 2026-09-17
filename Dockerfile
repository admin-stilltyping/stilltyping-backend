FROM ghcr.io/astral-sh/uv:0.9.10 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --no-editable && \
    useradd --system --uid 10001 --home-dir /app agent
USER agent
EXPOSE 8000
CMD ["python", "-m", "context_agent.bootstrap"]

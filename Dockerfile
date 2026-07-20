FROM ghcr.io/astral-sh/uv:0.9.24-python3.13-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY LICENSE NOTICE.md THIRD_PARTY_NOTICES.md ./
COPY src ./src
USER 65532:65532
EXPOSE 3978
CMD ["python", "-m", "agent_app.main"]

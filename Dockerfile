# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim-bookworm AS runtime

# The base image's own pip is never invoked at runtime (the app runs from
# the pre-built .venv below) but its vendored copies of msgpack/setuptools
# (pip/_vendor/) carry known CVEs that trivy flags regardless — removing an
# unused pip is a real fix, not a suppression of a finding we can't act on.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/alembic /app/alembic
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/README.md /app/README.md
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser data ./data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "tourism_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

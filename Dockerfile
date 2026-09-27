# syntax=docker/dockerfile:1

# The app image: the runtime base (Python, dependencies, torch, the embedding
# model; docker/runtime-base/Dockerfile) plus our code. CI passes the base tag
# that matches the current uv.lock; a local build without it uses :latest.
ARG RUNTIME_BASE=registry.gitlab.com/travel-platform2/tourism-backend/runtime-base:latest
FROM ${RUNTIME_BASE}

# Debian security updates published after the base was built. The base is
# rebuilt only when dependencies change, so without this the runtime keeps
# shipping whatever was current then, and trivy fails the pipeline the day an
# advisory lands (as on 2026-09-15 for libpcre2). CI passes the ISO week.
# The layer holds only the upgraded packages, a few MB.
ARG SECURITY_REFRESH=none
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Our code: small layers that change on a normal commit. Compiled here
# because the runtime filesystem is read-only and cannot cache bytecode.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini pyproject.toml README.md ./
RUN python -m compileall -q /app/src
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser data ./data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "tourism_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

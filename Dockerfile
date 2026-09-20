# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, source later: the heavy layers below (torch wheels, the
# embedding model download) depend only on pyproject.toml/uv.lock, so an
# ordinary code change reuses them from the build cache instead of
# re-downloading ~1 GB on every pipeline.
COPY pyproject.toml uv.lock README.md ./

# --extra rag: real local RAG embedder (sentence-transformers/torch, pinned
# to the CPU-only wheel index in pyproject.toml — see [tool.uv.sources]).
# Inert until RAG_ENABLED=true, but the image needs the package either way.
RUN uv sync --frozen --no-dev --no-install-project --extra rag

# Pre-download the embedding model at build time. The production container's
# filesystem is read-only, so sentence-transformers can't fetch/cache weights
# at request time — HF_HOME below pins the cache to a path independent of
# which user's $HOME is active in either stage (builder: root, runtime:
# appuser), and HF_HUB_OFFLINE in the runtime stage guarantees it never
# tries the network at all, matching read-only + no-egress expectations.
ENV HF_HOME=/app/.cache/huggingface
RUN .venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

# Only now bring in the project itself; this layer is small and the only one
# that changes on a normal commit.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --no-editable --extra rag

FROM python:3.13-slim-bookworm AS runtime

# Debian security updates published after the base image was cut. The image
# is pinned, so without this the runtime keeps shipping whatever was current
# when python:3.13-slim-bookworm was built, and trivy fails the pipeline the
# day an advisory lands — as it did on 2026-09-15 for libpcre2 (CVE-2026-86145,
# CVE-2026-89161), both already fixed in Debian. Upgrading is the actual fix;
# the alternative is waiting for someone else to rebuild the base image.
# CI passes the ISO week, so this layer (and the security fixes it pulls in)
# is refreshed weekly instead of being frozen inside the build cache.
ARG SECURITY_REFRESH=none
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

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
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/.cache /app/.cache
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

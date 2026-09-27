# syntax=docker/dockerfile:1

# Runtime base for the backend image: Python, every dependency from uv.lock
# (torch included) and the embedding model. It is ~2 GB and changes only with
# uv.lock or this file, so CI builds and pushes it only then, under a tag made
# from their hash (see backend-publish). The app image on top adds just the
# code. Before this split every commit rebuilt and re-uploaded the 2 GB to the
# registry from shared runners, and the upload kept failing (2026-09-27).
#
# Build context is the repository root.

FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# README.md stays out: pyproject names it as the readme, but
# --no-install-project never builds the package.
COPY pyproject.toml uv.lock ./

# --extra rag: real local RAG embedder (sentence-transformers/torch, pinned
# to the CPU-only wheel index in pyproject.toml, see [tool.uv.sources]).
# Two steps so torch (~770 MB) is a layer of its own and no single blob is
# ~1.5 GB: the registry drops uploads that big.
RUN uv sync --frozen --no-dev --no-install-project --extra rag --no-install-package torch
RUN uv sync --frozen --no-dev --no-install-project --extra rag

# The production filesystem is read-only, so the model is fetched here and
# the runtime runs with HF_HUB_OFFLINE.
ENV HF_HOME=/app/.cache/huggingface
RUN .venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

FROM python:3.13-slim-bookworm

# The base image's own pip is never invoked at runtime (the app runs from
# the pre-built .venv below) but its vendored copies of msgpack/setuptools
# (pip/_vendor/) carry known CVEs that trivy flags regardless. Removing an
# unused pip is a real fix, not a suppression.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY --from=builder --exclude=lib/python3.13/site-packages/torch /app/.venv /app/.venv
COPY --from=builder /app/.venv/lib/python3.13/site-packages/torch /app/.venv/lib/python3.13/site-packages/torch
COPY --from=builder --chown=appuser:appuser /app/.cache /app/.cache

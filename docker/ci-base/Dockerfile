# Base image for the lint/test/audit CI jobs (code-style, run-tests,
# security-tests, pip-audit). Every job used to apt-get curl/ca-certificates
# and curl-install uv from scratch on every single run — this bakes both in
# once so a normal pipeline run only pulls this image (already cached on the
# runner) instead of re-fetching and reinstalling the same packages.
#
# Rebuild + push whenever this file changes:
#   docker build -t registry.gitlab.com/travel-platform2/tourism-backend/ci-base:python3.13-uv docker/ci-base
#   docker push registry.gitlab.com/travel-platform2/tourism-backend/ci-base:python3.13-uv
# ...or just push a change to this file — backend-ci-base-image in
# .gitlab-ci.full.yml rebuilds and pushes it automatically.
FROM python:3.13-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:${PATH}"

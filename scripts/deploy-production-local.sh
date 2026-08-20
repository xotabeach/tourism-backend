#!/usr/bin/env bash
# Build amd64 locally, publish it to GitLab Registry, then run the existing
# pinned-host-key SSH deployment. This consumes no GitLab shared-runner minutes.

set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

: "${CI_REGISTRY:=registry.gitlab.com}"
: "${CI_REGISTRY_IMAGE:=registry.gitlab.com/travel-platform2/tourism-backend}"
: "${CI_REGISTRY_USER:?CI_REGISTRY_USER is required}"
: "${CI_REGISTRY_PASSWORD:?CI_REGISTRY_PASSWORD is required}"
: "${DEPLOY_SSH_HOST:?DEPLOY_SSH_HOST is required}"
: "${DEPLOY_SSH_PORT:?DEPLOY_SSH_PORT is required}"
: "${DEPLOY_SSH_USER:?DEPLOY_SSH_USER is required}"
: "${DEPLOY_SSH_PRIVATE_KEY:?DEPLOY_SSH_PRIVATE_KEY is required}"
: "${DEPLOY_SSH_KNOWN_HOSTS:?DEPLOY_SSH_KNOWN_HOSTS is required}"

CI_COMMIT_SHA="$(git rev-parse HEAD)"
export CI_COMMIT_SHA CI_REGISTRY CI_REGISTRY_IMAGE

image="${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}"
import_option="${1:-}"
if [[ -n "${import_option}" && "${import_option}" != "--import-osm-crimea" ]]; then
  printf 'Usage: %s [--import-osm-crimea]\n' "$0" >&2
  exit 2
fi

printf '%s\n' "${CI_REGISTRY_PASSWORD}" | docker login \
  --username "${CI_REGISTRY_USER}" \
  --password-stdin \
  "${CI_REGISTRY}"

docker buildx build \
  --platform linux/amd64 \
  --tag "${image}" \
  --tag "${CI_REGISTRY_IMAGE}:production" \
  --push \
  .

bash scripts/ci-deploy-production.sh "${image}" "${import_option}"

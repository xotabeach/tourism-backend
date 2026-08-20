#!/usr/bin/env bash
# Registry-independent fallback for a trusted developer machine.
# Builds the immutable amd64 image locally, transfers it through the pinned SSH
# alias, then runs the regular server migration/restart/health sequence.

set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

: "${DEPLOY_SSH_TARGET:=crimeatrip-test}"
: "${DEPLOY_HEALTH_URL:=https://86-106-20-132.sslip.io/health/ready}"
: "${CI_REGISTRY_IMAGE:=registry.gitlab.com/travel-platform2/tourism-backend}"

if [[ "${#}" -ne 0 ]]; then
  printf 'Usage: %s\n' "$0" >&2
  exit 2
fi

commit_sha="$(git rev-parse HEAD)"
image="${CI_REGISTRY_IMAGE}:${commit_sha}"

printf 'Building local production image: %s\n' "${image}"
docker buildx build \
  --platform linux/amd64 \
  --tag "${image}" \
  --load \
  .

printf 'Transferring image to SSH target: %s\n' "${DEPLOY_SSH_TARGET}"
docker save "${image}" | gzip -1 | \
  ssh "${DEPLOY_SSH_TARGET}" 'gunzip | docker load'

printf 'Running migrations and recreating backend\n'
ssh "${DEPLOY_SSH_TARGET}" \
  "DEPLOY_SKIP_PULL=true DEPLOY_HEALTH_URL=$(printf '%q' "${DEPLOY_HEALTH_URL}") \
   /opt/crimeatrip-test/deploy-remote.sh $(printf '%q' "${image}")"

printf 'Direct production deploy finished: %s\n' "${image}"

#!/usr/bin/env bash
# Registry-independent fallback for a trusted developer machine.
# Builds the immutable amd64 image locally, transfers it through the pinned SSH
# alias, then runs the regular server migration/restart/health sequence.

set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

# Адреса серверов в репозитории не хранятся: они лежат в scripts/deploy.env
# (в .gitignore) или передаются переменными окружения. Значения по умолчанию
# здесь были бы утечкой инфраструктуры в публичное зеркало на GitHub — и,
# отдельно, ловушкой: раньше по умолчанию стоял выведенный из обращения хост,
# и деплой туда проходил с кодом 0, ничего не публикуя (2026-09-04).
if [[ -f "${PROJECT_ROOT}/scripts/deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "${PROJECT_ROOT}/scripts/deploy.env"
fi

: "${DEPLOY_SSH_TARGET:?DEPLOY_SSH_TARGET is required (ssh alias of the target host)}"
: "${DEPLOY_HEALTH_URL:?DEPLOY_HEALTH_URL is required (https://<api-host>/health/ready)}"
: "${DEPLOY_REMOTE_DIR:?DEPLOY_REMOTE_DIR is required (compose directory on the server)}"
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
   $(printf '%q' "${DEPLOY_REMOTE_DIR}")/deploy-remote.sh $(printf '%q' "${image}")"

printf 'Direct production deploy finished: %s\n' "${image}"

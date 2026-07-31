#!/usr/bin/env bash
# GitLab CI helper: SSH into the production host and run deploy-remote.sh.
# Runs only from the production deploy job on branch main.
#
# Required protected CI variables:
#   DEPLOY_SSH_HOST, DEPLOY_SSH_PORT, DEPLOY_SSH_USER, DEPLOY_SSH_PRIVATE_KEY
#   DEPLOY_SSH_KNOWN_HOSTS  — full known_hosts line(s) or GitLab File var path.
#     Pin it once with:  ssh-keyscan -p <port> -H <host>
# Optional:
#   DEPLOY_HEALTH_URL       — forwarded to the remote script
#
# GitLab File-type variables expand to a temp file path, not the raw content.

set -Eeuo pipefail

IMAGE="${1:-${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}}"

: "${DEPLOY_SSH_HOST:?DEPLOY_SSH_HOST is required}"
: "${DEPLOY_SSH_PORT:?DEPLOY_SSH_PORT is required}"
: "${DEPLOY_SSH_USER:?DEPLOY_SSH_USER is required}"
: "${DEPLOY_SSH_PRIVATE_KEY:?DEPLOY_SSH_PRIVATE_KEY is required}"
# Without a pinned host key, ssh-keyscan would trust whatever answers and
# StrictHostKeyChecking below would verify nothing.
: "${DEPLOY_SSH_KNOWN_HOSTS:?DEPLOY_SSH_KNOWN_HOSTS is required (pin the host key)}"

materialize_secret() {
  local value="$1"
  local dest="$2"
  if [[ -f "${value}" ]]; then
    cp "${value}" "${dest}"
  else
    printf '%s\n' "${value}" > "${dest}"
  fi
  chmod 600 "${dest}"
}

install -d -m 700 ~/.ssh
key_file="$(mktemp)"
chmod 600 "${key_file}"
materialize_secret "${DEPLOY_SSH_PRIVATE_KEY}" "${key_file}"

materialize_secret "${DEPLOY_SSH_KNOWN_HOSTS}" ~/.ssh/known_hosts
chmod 644 ~/.ssh/known_hosts

ssh_opts=(
  -i "${key_file}"
  -p "${DEPLOY_SSH_PORT}"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
)

remote_env=()
if [[ -n "${DEPLOY_HEALTH_URL:-}" ]]; then
  remote_env+=("DEPLOY_HEALTH_URL=$(printf '%q' "${DEPLOY_HEALTH_URL}")")
fi

printf 'Production deploy %s to %s@%s:%s\n' \
  "${IMAGE}" "${DEPLOY_SSH_USER}" "${DEPLOY_SSH_HOST}" "${DEPLOY_SSH_PORT}"

# shellcheck disable=SC2029
ssh "${ssh_opts[@]}" "${DEPLOY_SSH_USER}@${DEPLOY_SSH_HOST}" \
  "${remote_env[*]} /opt/crimeatrip-test/deploy-remote.sh $(printf '%q' "${IMAGE}")"

rm -f "${key_file}"
printf 'Production deploy finished.\n'

#!/usr/bin/env bash
# SSH helper shared by GitLab CI and the trusted local production deploy.
#
# Required protected CI variables:
#   DEPLOY_SSH_HOST, DEPLOY_SSH_PORT, DEPLOY_SSH_USER, DEPLOY_SSH_PRIVATE_KEY
#   DEPLOY_SSH_KNOWN_HOSTS  — full known_hosts line(s) or GitLab File var path.
#     Pin it once with:  ssh-keyscan -p <port> -H <host>
# Optional:
#   DEPLOY_HEALTH_URL       — forwarded to the remote script
#   --import-osm-crimea     — import 1000 OSM candidates as drafts after deploy
#
# Registry pull on the host uses CI_REGISTRY_* from the job (not a long-lived
# server-side docker login).
#
# GitLab File-type variables expand to a temp file path, not the raw content.

set -Eeuo pipefail

IMAGE="${1:-${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}}"
IMPORT_OSM_CRIMEA=false
case "${2:-}" in
  "") ;;
  --import-osm-crimea) IMPORT_OSM_CRIMEA=true ;;
  *)
    printf 'Error: unsupported option: %s\n' "${2}" >&2
    exit 2
    ;;
esac

: "${DEPLOY_SSH_HOST:?DEPLOY_SSH_HOST is required}"
: "${DEPLOY_SSH_PORT:?DEPLOY_SSH_PORT is required}"
: "${DEPLOY_SSH_USER:?DEPLOY_SSH_USER is required}"
: "${DEPLOY_SSH_PRIVATE_KEY:?DEPLOY_SSH_PRIVATE_KEY is required}"
# Without a pinned host key, ssh-keyscan would trust whatever answers and
# StrictHostKeyChecking below would verify nothing.
: "${DEPLOY_SSH_KNOWN_HOSTS:?DEPLOY_SSH_KNOWN_HOSTS is required (pin the host key)}"
: "${CI_REGISTRY:?CI_REGISTRY is required}"
: "${CI_REGISTRY_USER:?CI_REGISTRY_USER is required}"
: "${CI_REGISTRY_PASSWORD:?CI_REGISTRY_PASSWORD is required}"

# Write an OpenSSH private key that Alpine/OpenSSL will accept.
# File variables are preferred; env values may arrive with literal \n sequences.
materialize_openssh_key() {
  local value="$1"
  local dest="$2"
  local content

  if [[ -f "${value}" ]]; then
    content="$(cat "${value}")"
  else
    content="${value}"
  fi

  # Strip CR; expand one-line keys that used literal \n.
  content="${content//$'\r'/}"
  if [[ "${content}" != *$'\n'* ]]; then
    content="${content//\\n/$'\n'}"
  fi

  # Exact bytes — no extra printf escaping of the PEM body.
  printf '%s' "${content}" > "${dest}"
  if [[ -s "${dest}" ]] && [[ "$(tail -c1 "${dest}" | wc -l)" -eq 0 ]]; then
    printf '\n' >> "${dest}"
  fi
  chmod 600 "${dest}"

  # Fail closed with a clear message instead of ssh's opaque libcrypto error.
  if ! ssh-keygen -y -f "${dest}" >/dev/null 2>&1; then
    printf 'Error: DEPLOY_SSH_PRIVATE_KEY is not a usable OpenSSH private key.\n' >&2
    printf 'Re-upload it as a GitLab File variable from a PEM/OpenSSH key file.\n' >&2
    exit 1
  fi
}

materialize_secret_file() {
  local value="$1"
  local dest="$2"
  if [[ -f "${value}" ]]; then
    cp "${value}" "${dest}"
  else
    printf '%s\n' "${value}" > "${dest}"
  fi
  chmod 600 "${dest}"
}

deploy_tmp_dir="$(mktemp -d)"
key_file="${deploy_tmp_dir}/deploy_key"
known_hosts_file="${deploy_tmp_dir}/known_hosts"
trap 'rm -f -- "${key_file}" "${known_hosts_file}"; rmdir -- "${deploy_tmp_dir}"' EXIT
touch "${key_file}"
chmod 600 "${key_file}"
materialize_openssh_key "${DEPLOY_SSH_PRIVATE_KEY}" "${key_file}"

materialize_secret_file "${DEPLOY_SSH_KNOWN_HOSTS}" "${known_hosts_file}"
chmod 600 "${known_hosts_file}"

ssh_opts=(
  -i "${key_file}"
  -p "${DEPLOY_SSH_PORT}"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="${known_hosts_file}"
)

remote_env=()
if [[ -n "${DEPLOY_HEALTH_URL:-}" ]]; then
  remote_env+=("DEPLOY_HEALTH_URL=$(printf '%q' "${DEPLOY_HEALTH_URL}")")
fi

printf 'Production deploy %s to %s@%s:%s\n' \
  "${IMAGE}" "${DEPLOY_SSH_USER}" "${DEPLOY_SSH_HOST}" "${DEPLOY_SSH_PORT}"

# Login on the host with this job's registry credentials, then pull/migrate.
# shellcheck disable=SC2029
ssh "${ssh_opts[@]}" "${DEPLOY_SSH_USER}@${DEPLOY_SSH_HOST}" \
  "CI_REGISTRY=$(printf '%q' "${CI_REGISTRY}") \
   CI_REGISTRY_USER=$(printf '%q' "${CI_REGISTRY_USER}") \
   CI_REGISTRY_PASSWORD=$(printf '%q' "${CI_REGISTRY_PASSWORD}") \
   IMAGE=$(printf '%q' "${IMAGE}") \
   IMPORT_OSM_CRIMEA=$(printf '%q' "${IMPORT_OSM_CRIMEA}") \
   ${remote_env[*]} \
   bash -s" <<'EOS'
set -Eeuo pipefail
printf '%s\n' "${CI_REGISTRY_PASSWORD}" | docker login \
  -u "${CI_REGISTRY_USER}" \
  --password-stdin \
  "${CI_REGISTRY}"
# The helper starts Compose commands that may read stdin. Keep it away from
# this bash heredoc, otherwise it can consume the import commands below.
/opt/crimeatrip-test/deploy-remote.sh "${IMAGE}" </dev/null

if [[ "${IMPORT_OSM_CRIMEA}" == "true" ]]; then
  printf 'Starting server-side OSM Crimea import.\n'
  cd /opt/crimeatrip-test
  docker compose --env-file .env --file compose.yaml run --rm -T --no-deps backend \
    python scripts/seed_crimea.py --categories-only
  docker compose --env-file .env --file compose.yaml run --rm -T --no-deps backend \
    python scripts/import_osm_crimea.py \
      --fetch \
      --limit 1000 \
      --apply \
      --no-cache

  docker compose --env-file .env --file compose.yaml exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT publication_status, data_quality_status, count(*) FROM places WHERE source_name = '\''openstreetmap'\'' GROUP BY publication_status, data_quality_status ORDER BY publication_status, data_quality_status;"'
fi
EOS

printf 'Production deploy finished.\n'

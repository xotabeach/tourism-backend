#!/usr/bin/env bash
# Mirror the current GitLab commit to the GitHub showcase remote.
# Intended for GitLab CI. Requires:
#   GITHUB_MIRROR_TOKEN  — GitHub PAT / fine-grained token with contents:write
# Optional:
#   GITHUB_MIRROR_OWNER  — defaults to xotabeach
#   GITHUB_MIRROR_REPO   — defaults to CI_PROJECT_NAME
#   GITHUB_MIRROR_FORCE  — set to 1 to force-push (default on; GitLab is source of truth)

set -Eeuo pipefail

if [[ -z "${GITHUB_MIRROR_TOKEN:-}" ]]; then
  printf 'GITHUB_MIRROR_TOKEN is not set; skipping GitHub showcase mirror.\n'
  exit 0
fi

OWNER="${GITHUB_MIRROR_OWNER:-xotabeach}"
REPO="${GITHUB_MIRROR_REPO:-${CI_PROJECT_NAME:?CI_PROJECT_NAME is required}}"
BRANCH="${CI_COMMIT_REF_NAME:?CI_COMMIT_REF_NAME is required}"
REMOTE_URL="https://x-access-token:${GITHUB_MIRROR_TOKEN}@github.com/${OWNER}/${REPO}.git"
FORCE_FLAG=()

if [[ "${GITHUB_MIRROR_FORCE:-1}" == "1" ]]; then
  FORCE_FLAG=(--force)
fi

git remote remove github 2>/dev/null || true
git remote add github "${REMOTE_URL}"

printf 'Mirroring %s@%s → github.com/%s/%s\n' \
  "${REPO}" "${BRANCH}" "${OWNER}" "${REPO}"

# Keep the working branch name on GitHub.
git push "${FORCE_FLAG[@]}" github "HEAD:refs/heads/${BRANCH}"

# Showcase default branch is main; keep it aligned with gamma/main.
if [[ "${BRANCH}" == "gamma" || "${BRANCH}" == "main" ]]; then
  git push "${FORCE_FLAG[@]}" github "HEAD:refs/heads/main"
fi

printf 'GitHub showcase mirror updated.\n'

#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'Error: required command "%s" not found.\n' "${command_name}" >&2
    exit 1
  fi
}

require_command uv

cd "${PROJECT_ROOT}"

printf 'Syncing dependencies...\n'
uv sync --all-extras --dev

printf 'Running Ruff...\n'
uv run -- ruff check .
uv run -- ruff format --check .

printf 'Running MyPy...\n'
uv run mypy src/tourism_backend

printf 'Running pip-audit...\n'
# uv sync may leave a vulnerable tooling pip; bump before audit.
uv pip install 'pip>=26.2' >/dev/null
uv run pip-audit

printf 'Running Pytest...\n'
uv run pytest

printf 'Validation completed successfully.\n'

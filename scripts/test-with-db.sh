#!/usr/bin/env bash
# Runs the backend tests against a throwaway database (BACKEND-49).
#
# The tests need PostgreSQL with PostGIS and pgvector plus Redis. Pointed at
# the development database they leave rows behind and depend on what is
# already there, and without a database ~50 of them fail. This script starts
# the same database image as CI (docker/ci-postgres) and a Redis in their own
# containers, creates a fresh database, migrates and seeds it, then runs
# pytest with the arguments given (all tests by default).
#
#   scripts/test-with-db.sh                       # everything
#   scripts/test-with-db.sh tests/unit -k rating  # any pytest arguments
#   TEST_KEEP_DB=1 scripts/test-with-db.sh ...    # reuse the database
#
# Containers stay up between runs; `docker rm -f tourism-test-pg
# tourism-test-redis` removes them.
set -euo pipefail

cd "$(dirname "$0")/.."

PG_CONTAINER="${TEST_PG_CONTAINER:-tourism-test-pg}"
REDIS_CONTAINER="${TEST_REDIS_CONTAINER:-tourism-test-redis}"
PG_PORT="${TEST_PG_PORT:-5544}"
REDIS_PORT="${TEST_REDIS_PORT:-6390}"
DB_NAME="${TEST_DB_NAME:-tourism_test}"
PG_IMAGE="tourism-ci-postgres:16"
PASSWORD="local-tourism-password"

if ! docker image inspect "${PG_IMAGE}" >/dev/null 2>&1; then
  echo "==> Building ${PG_IMAGE} from docker/ci-postgres"
  docker build -q -t "${PG_IMAGE}" docker/ci-postgres >/dev/null
fi

start() {
  local name="$1"; shift
  if [[ -z "$(docker ps -q --filter "name=^${name}$")" ]]; then
    docker rm -f "${name}" >/dev/null 2>&1 || true
    echo "==> Starting ${name}"
    docker run -d --name "${name}" "$@" >/dev/null
  fi
}

start "${PG_CONTAINER}" -e POSTGRES_USER=tourism -e POSTGRES_PASSWORD="${PASSWORD}" \
  -e POSTGRES_DB=postgres -p "127.0.0.1:${PG_PORT}:5432" "${PG_IMAGE}"
start "${REDIS_CONTAINER}" -p "127.0.0.1:${REDIS_PORT}:6379" redis:7-alpine

for _ in $(seq 1 30); do
  docker exec "${PG_CONTAINER}" pg_isready -U tourism -q && break
  sleep 1
done

export APP_ENV=test
export DATABASE_URL="postgresql+asyncpg://tourism:${PASSWORD}@localhost:${PG_PORT}/${DB_NAME}"
export DATABASE_URL_SYNC="postgresql+psycopg://tourism:${PASSWORD}@localhost:${PG_PORT}/${DB_NAME}"
export REDIS_URL="redis://localhost:${REDIS_PORT}/15"

exists="$(docker exec "${PG_CONTAINER}" psql -U tourism -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")"
if [[ "${TEST_KEEP_DB:-}" != "1" || "${exists}" != "1" ]]; then
  echo "==> Fresh database ${DB_NAME}"
  docker exec "${PG_CONTAINER}" psql -U tourism -d postgres -qc \
    "DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)" -c "CREATE DATABASE ${DB_NAME}"
  docker exec "${REDIS_CONTAINER}" redis-cli -n 15 FLUSHDB >/dev/null
  uv run alembic upgrade head >/dev/null
  uv run python scripts/seed_crimea.py
fi

if [[ $# -eq 0 ]]; then
  set -- tests
fi
exec uv run pytest -q -p no:cacheprovider --no-cov "$@"

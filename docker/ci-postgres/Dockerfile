# Postgres 16 with pgvector and PostGIS for the CI jobs that need a database.
#
# The jobs used to start postgis/postgis:16-3.4 and apt-install pgvector on
# every run. That image is Debian bullseye, and on 2026-10-07 the bullseye
# package repository of PostgreSQL was removed: apt-get update failed, the
# database never started and every test job died at "postgres did not become
# reachable". Both extensions are baked in here instead, on a supported
# Debian, so a job only pulls the image.
FROM pgvector/pgvector:pg16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 \
    && rm -rf /var/lib/apt/lists/*

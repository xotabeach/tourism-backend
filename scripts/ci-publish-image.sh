#!/bin/sh
# Build and push the backend image in two parts (docker/runtime-base/Dockerfile,
# Dockerfile). The ~2 GB runtime base is built and pushed only when no image
# with the hash of uv.lock and its Dockerfile exists yet; the app image on top
# is tens of MB. Plain docker build/push: push retries each layer itself and
# never uploads the same blob twice at once, which the registry cache export
# did and the registry answered with 400 Bad Request.
#
#   scripts/ci-publish-image.sh <tag> [<tag> ...]
set -eu

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <tag> [<tag> ...]" >&2
    exit 2
fi

base_hash=$(cat uv.lock docker/runtime-base/Dockerfile | sha256sum | cut -c1-16)
base="$CI_REGISTRY_IMAGE/runtime-base:$base_hash"

push() {
    # The registry drops a big upload now and then; layers already uploaded
    # are skipped on the next attempt, so a retry only redoes what failed.
    for attempt in 1 2 3; do
        if docker push "$1"; then
            return 0
        fi
        echo "push of $1 failed (attempt $attempt), retrying" >&2
        sleep 20
    done
    return 1
}

if docker manifest inspect "$base" >/dev/null 2>&1; then
    echo "runtime base $base_hash is already in the registry"
else
    echo "building runtime base $base_hash"
    docker build --pull -f docker/runtime-base/Dockerfile -t "$base" .
    push "$base"
    if [ "${CI_COMMIT_BRANCH:-}" = "main" ]; then
        docker tag "$base" "$CI_REGISTRY_IMAGE/runtime-base:latest"
        push "$CI_REGISTRY_IMAGE/runtime-base:latest"
    fi
fi

first="$1"
tag_args=""
for tag in "$@"; do
    tag_args="$tag_args -t $tag"
done
# shellcheck disable=SC2086  # tag_args is a list of -t flags
docker build \
    --build-arg RUNTIME_BASE="$base" \
    --build-arg SECURITY_REFRESH="$(date +%G-%V)" \
    $tag_args .
for tag in "$@"; do
    push "$tag"
done
echo "published $first on runtime base $base_hash"

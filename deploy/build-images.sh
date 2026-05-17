#!/bin/sh
# Build (and push) the 6 dataviz container images to a registry.
#
#   REGISTRY  registry host           (default: docker.io)
#   ORG       org / namespace         (default: synodic)
#   TAG       image tag               (default: latest)
#   PUSH      push after build        (default: true; set false to build only)
#
# Examples:
#   sh deploy/build-images.sh
#   REGISTRY=docker.io ORG=myorg TAG=v1.2.3 sh deploy/build-images.sh
#   PUSH=false TAG=test sh deploy/build-images.sh
#
# Run from the repo root (build context is the repo root for every image).
set -eu

REGISTRY="${REGISTRY:-docker.io}"
ORG="${ORG:-synodic}"
TAG="${TAG:-latest}"
PUSH="${PUSH:-true}"

# name<TAB>dockerfile  (build context is always ".")
IMAGES="
viz-service	backend/Dockerfile.viz
graph-service	backend/Dockerfile.graph
aggregation-controlplane	backend/Dockerfile.controlplane
aggregation-worker	backend/Dockerfile.aggregation
stats-service	backend/Dockerfile.insights
frontend	frontend/Dockerfile
seed	backend/Dockerfile.seed
"

if [ ! -f docker-compose.yml ]; then
  echo "ERROR: run this from the repo root (docker-compose.yml not found)." >&2
  exit 1
fi

echo "$IMAGES" | while IFS='	' read -r name dockerfile; do
  [ -z "$name" ] && continue
  ref="${REGISTRY}/${ORG}/${name}:${TAG}"
  echo "==> build ${ref}  (-f ${dockerfile})"
  docker build -f "${dockerfile}" -t "${ref}" .
  if [ "${PUSH}" = "true" ]; then
    echo "==> push  ${ref}"
    docker push "${ref}"
  fi
done

echo "Done. Images tagged ${REGISTRY}/${ORG}/<name>:${TAG}"

#!/usr/bin/env bash
# Build the pilot controller from a pinned commit. Reproducible by
# construction: the context is a `git archive` of that commit, so the working
# tree cannot contribute a byte.
#
#   ./docker/pilot-controller/build.sh ad13cf0 apoapsis-pilot-controller:ad13cf0
#
# Prints the build-context digest, the resulting image id, and the labels the
# image carries, all of which belong in the pilot manifest.
set -euo pipefail

COMMIT="${1:?usage: build.sh <commit> [tag]}"
TAG="${2:-apoapsis-pilot-controller:${COMMIT}}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
DOCKERFILE="${REPO_ROOT}/docker/pilot-controller/Dockerfile"

FULL_COMMIT="$(git -C "${REPO_ROOT}" rev-parse "${COMMIT}^{commit}")"
TREE="$(git -C "${REPO_ROOT}" rev-parse "${COMMIT}^{tree}")"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# `git archive` writes only committed bytes. This is the whole provenance
# argument: an uncommitted edit has no route into the context.
#
# The pathspec is declared rather than implicit. `spikes/native-shell-tauri`
# carries roughly 800MB of committed Rust build artifacts; including them would
# make the context digest mostly bytes the controller never reads.
git -C "${REPO_ROOT}" archive --format=tar "${FULL_COMMIT}" \
  -- src pyproject.toml README.md LICENSE.txt \
  > "${WORK}/context.tar"
CONTEXT_SHA="$(sha256sum "${WORK}/context.tar" | cut -d' ' -f1)"

mkdir -p "${WORK}/ctx"
tar -C "${WORK}/ctx" -xf "${WORK}/context.tar"
cp "${DOCKERFILE}" "${WORK}/ctx/Dockerfile.pilot"

echo "source_commit:        ${FULL_COMMIT}"
echo "source_tree:          ${TREE}"
echo "build_context_sha256: ${CONTEXT_SHA}"
echo "dockerfile_sha256:    $(sha256sum "${DOCKERFILE}" | cut -d' ' -f1)"

# --no-cache is correctness, not caution: a cached LABEL layer retains the
# build args of whichever build first created it, so a rebuild can carry a
# build-context digest belonging to a different context.
docker build --no-cache \
  --file "${WORK}/ctx/Dockerfile.pilot" \
  --build-arg "SOURCE_COMMIT=${FULL_COMMIT}" \
  --build-arg "SOURCE_TREE=${TREE}" \
  --build-arg "BUILD_CONTEXT_SHA256=${CONTEXT_SHA}" \
  --tag "${TAG}" \
  "${WORK}/ctx"

echo "image_id:             $(docker image inspect "${TAG}" --format '{{.Id}}')"
echo "labels:               $(docker image inspect "${TAG}" --format '{{json .Config.Labels}}')"

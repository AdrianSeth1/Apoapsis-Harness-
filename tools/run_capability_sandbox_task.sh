#!/usr/bin/env bash
set -euo pipefail

REPO="$1"
SEED="$2"
REQUEST="$3"
RESPONSE="$4"

if ! git -C "${REPO}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Capability Sandbox harness path is not a Git worktree: ${REPO}" >&2
  exit 2
fi
if ! git -C "${SEED}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Capability Sandbox task path is not a Git worktree: ${SEED}" >&2
  exit 2
fi
if ! test -f "${REQUEST}"; then
  echo "Capability Sandbox request is missing: ${REQUEST}" >&2
  exit 2
fi

# Verdict-deciding product code is built only from the current committed
# source tree.  A dirty harness checkout cannot quietly decide a task.
if test -n "$(git -C "${REPO}" status --porcelain --untracked-files=all -- src pyproject.toml tools/run_capability_sandbox_task.sh docker/pilot-controller docs/qualification)"; then
  echo "Capability Sandbox product code has uncommitted changes; commit the Apoapsis update before it decides a task." >&2
  exit 3
fi

if test "${5:-}" = "--preflight-only"; then
  echo "Capability Sandbox preflight passed."
  exit 0
fi

COMMIT="$(git -C "${REPO}" rev-parse HEAD)"
TAG="apoapsis-product-controller:${COMMIT:0:12}"
if ! docker image inspect "${TAG}" >/dev/null 2>&1; then
  bash "${REPO}/docker/pilot-controller/build.sh" "${COMMIT}" "${TAG}"
fi

RUNTIME="$(dirname "${RESPONSE}")/docker-runtime"
mkdir -p "${RUNTIME}"

docker run --rm --pull never --network host --gpus all \
  --name "apoapsis-product-${COMMIT:0:8}-$$" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${REPO}:${REPO}:ro" \
  -v "${SEED}:${SEED}:ro" \
  -v "$(dirname "${RESPONSE}"):$(dirname "${RESPONSE}"):rw" \
  -v /home/arya/llama.cpp:/home/arya/llama.cpp:ro \
  -v /home/arya/models:/home/arya/models:ro \
  -v /usr/local/cuda:/usr/local/cuda:ro \
  "${TAG}" -m apoapsis.workcell.product_live \
  --repo "${REPO}" --seed "${SEED}" --request "${REQUEST}" --response "${RESPONSE}"

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
if ! test -f "${REQUEST}"; then
  echo "Capability Sandbox request is missing: ${REQUEST}" >&2
  exit 2
fi

# A worktree created by Windows Git contains a `.git` *file* whose `gitdir:`
# target is a Windows absolute path. Linux Git interprets that as relative text
# and cannot open it. Resolve the pointer explicitly, without modifying the
# operator's worktree, then make a normal disposable Linux-readable clone in
# the response runtime directory for the controller/container.
SEED_DOT_GIT="${SEED}/.git"
if test -d "${SEED_DOT_GIT}"; then
  SEED_GIT_DIR="${SEED_DOT_GIT}"
elif test -f "${SEED_DOT_GIT}"; then
  SEED_GIT_POINTER="$(sed -n 's/^gitdir: //p' "${SEED_DOT_GIT}")"
  if test -z "${SEED_GIT_POINTER}"; then
    echo "Capability Sandbox task worktree has an invalid .git pointer: ${SEED}" >&2
    exit 2
  fi
  if [[ "${SEED_GIT_POINTER}" =~ ^[A-Za-z]:[/\\] ]]; then
    SEED_GIT_DIR="$(wslpath -u "${SEED_GIT_POINTER}")"
  elif [[ "${SEED_GIT_POINTER}" = /* ]]; then
    SEED_GIT_DIR="${SEED_GIT_POINTER}"
  else
    SEED_GIT_DIR="$(realpath -m "${SEED}/${SEED_GIT_POINTER}")"
  fi
else
  echo "Capability Sandbox task path is not a Git worktree: ${SEED}" >&2
  exit 2
fi
if ! git --git-dir="${SEED_GIT_DIR}" --work-tree="${SEED}" \
  rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Capability Sandbox task Git metadata is unreadable: ${SEED_GIT_DIR}" >&2
  exit 2
fi
if test -n "$(git --git-dir="${SEED_GIT_DIR}" --work-tree="${SEED}" status --porcelain --untracked-files=all)"; then
  echo "Capability Sandbox task worktree must be unchanged before launch: ${SEED}" >&2
  exit 2
fi

# Verdict-deciding product code is built only from the current committed
# source tree.  A dirty harness checkout cannot quietly decide a task.
if test -n "$(git -C "${REPO}" status --porcelain --untracked-files=all -- src pyproject.toml tools/run_capability_sandbox_task.sh docker/pilot-controller docs/qualification)"; then
  echo "Capability Sandbox product code has uncommitted changes; commit the Apoapsis update before it decides a task." >&2
  exit 3
fi

COMMIT="$(git -C "${REPO}" rev-parse HEAD)"
TAG="apoapsis-product-controller:${COMMIT:0:12}"
RUNTIME="$(mktemp -d /tmp/apx-product-XXXXXXXX)"
trap 'rm -rf "${RUNTIME}"' EXIT
NORMALIZED_SEED="${RUNTIME}/seed"
SEED_COMMIT="$(git --git-dir="${SEED_GIT_DIR}" --work-tree="${SEED}" rev-parse HEAD)"
SEED_COMMON_DIR="$(git --git-dir="${SEED_GIT_DIR}" rev-parse --git-common-dir)"
git clone --quiet --no-local "${SEED_COMMON_DIR}" "${NORMALIZED_SEED}"
git -C "${NORMALIZED_SEED}" checkout --quiet --detach "${SEED_COMMIT}"

if test "${5:-}" = "--preflight-only"; then
  echo "Capability Sandbox preflight passed."
  exit 0
fi

if ! docker image inspect "${TAG}" >/dev/null 2>&1; then
  bash "${REPO}/docker/pilot-controller/build.sh" "${COMMIT}" "${TAG}"
fi

docker run --rm --pull never --network host --gpus all \
  --name "apoapsis-product-${COMMIT:0:8}-$$" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${REPO}:${REPO}:ro" \
  -v "$(dirname "${RESPONSE}"):$(dirname "${RESPONSE}"):rw" \
  -v "${RUNTIME}:${RUNTIME}:rw" \
  -v /home/arya/llama.cpp:/home/arya/llama.cpp:ro \
  -v /home/arya/models:/home/arya/models:ro \
  -v /usr/local/cuda:/usr/local/cuda:ro \
  "${TAG}" -m apoapsis.workcell.product_live \
  --repo "${REPO}" --seed "${NORMALIZED_SEED}" --request "${REQUEST}" \
  --response "${RESPONSE}" --runtime-root "${RUNTIME}/controller"

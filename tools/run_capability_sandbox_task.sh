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

# The controller starts its own pinned llama-server for this run. A second one
# already resident is not a spare: it is another full copy of the same weights
# on the same GPU, and two 16GB copies do not fit on a 24GB card. The loser is
# evicted mid-generation, which reaches the relay as "the workcell closed the
# connection mid-response" -- indistinguishable, from there, from the coding
# model having failed.
#
# `verify_runtime` already refuses this, but it runs inside the controller
# container and reads that container's `/proc`, so a server started on the host
# is invisible to it and the guard cannot fire. This is the same check at the
# only layer that can see both.
RESIDENT_LLAMA=""
for COMM_FILE in /proc/[0-9]*/comm; do
  test -r "${COMM_FILE}" || continue
  test "$(cat "${COMM_FILE}" 2>/dev/null)" = "llama-server" || continue
  RESIDENT_LLAMA="${RESIDENT_LLAMA} $(basename "$(dirname "${COMM_FILE}")")"
done
if test -n "${RESIDENT_LLAMA}"; then
  echo "Capability Sandbox needs the GPU to itself: llama-server is already running (PID${RESIDENT_LLAMA})." >&2
  for PID in ${RESIDENT_LLAMA}; do
    test -r "/proc/${PID}/cmdline" || continue
    echo "  PID ${PID}: $(tr '\0' ' ' < "/proc/${PID}/cmdline")" >&2
  done
  echo "The controller starts its own pinned server; a second copy of the weights will not fit alongside it." >&2
  echo "Stop the resident server (kill ${RESIDENT_LLAMA# }), then start this slice again." >&2
  exit 4
fi

COMMIT="$(git -C "${REPO}" rev-parse HEAD)"
TAG="apoapsis-product-controller:${COMMIT:0:12}"
RUNTIME="$(mktemp -d /tmp/apx.XXXXXX)"
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

CONTROLLER_EXTRA_ARGS=()
if test "${5:-}" = "--containment-preflight-only"; then
  CONTROLLER_EXTRA_ARGS+=("--containment-preflight-only")
fi

if ! docker image inspect "${TAG}" >/dev/null 2>&1; then
  bash "${REPO}/docker/pilot-controller/build.sh" "${COMMIT}" "${TAG}" "${REPO}"
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
  --response "${RESPONSE}" --runtime-root "${RUNTIME}/r" \
  "${CONTROLLER_EXTRA_ARGS[@]}"

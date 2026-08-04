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
# The porcelain output is the diagnosis, not decoration. Reporting only the
# path costs hours: on 2026-08-02 this guard refused a launch while Windows
# Git called the same worktree clean, and the cause was invisible until the
# file list was read. Git for Windows sets core.autocrlf=true in its *system*
# config, which WSL's Git never reads, so a CRLF working tree matched
# LF blobs for one and differed for the other. Every tracked file appearing
# as modified is that signature, which is why the hint below is worth a line.
SEED_STATUS="$(git --git-dir="${SEED_GIT_DIR}" --work-tree="${SEED}" status --porcelain --untracked-files=all)"
if test -n "${SEED_STATUS}"; then
  echo "Capability Sandbox task worktree must be unchanged before launch: ${SEED}" >&2
  echo "${SEED_STATUS}" | head -40 >&2
  SEED_CHANGED_COUNT="$(printf '%s\n' "${SEED_STATUS}" | wc -l | tr -d ' ')"
  if test "${SEED_CHANGED_COUNT}" -gt 40; then
    echo "  ... and $((SEED_CHANGED_COUNT - 40)) more" >&2
  fi
  echo "If every tracked file above is listed as modified ( M ), compare line" >&2
  echo "endings before assuming real edits: 'git -C \"${SEED}\" diff --stat' from" >&2
  echo "Windows and from WSL can disagree when core.autocrlf differs between" >&2
  echo "them. Setting core.autocrlf=false on the project repository and" >&2
  echo "re-checking out the worktree normalizes it for both." >&2
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

# The controller image build is the one part of a slice that happens *before*
# the controller exists, so the controller cannot record it. It is also the
# longest opaque wait a first run can hit: the image is tagged by harness
# commit and built `--no-cache`, so any commit to Apoapsis makes the next slice
# pay a full build with nothing on screen to say so.
#
# These two lines put that stage in the same journal the controller appends to
# (MH-9). Written in shell rather than through Python on purpose: the thing
# that performs the build is the thing that should record it, and this runs on
# the host before any container starts.
EVIDENCE_DIR="$(dirname "${RESPONSE}")/evidence"
PROGRESS="${EVIDENCE_DIR}/progress.jsonl"
mkdir -p "${EVIDENCE_DIR}"

progress_event() {
  # $1 kind, $2 payload-json. Best-effort: a journal that cannot be written
  # must never stop a slice that was otherwise going to run.
  local next=1
  # `test -f` first: redirecting stdin from a file that does not exist is a
  # *shell* error, printed before `wc` starts, so `2>/dev/null` on `wc` cannot
  # suppress it. Without this the very first event of every run writes a
  # spurious "No such file or directory" into the launch log.
  if test -f "${PROGRESS}"; then
    next=$(( $(wc -l < "${PROGRESS}") + 1 ))
  fi
  printf '{"sequence": %d, "at": "%s", "kind": "%s", "stage": "controller_build", "payload": %s}\n' \
    "${next}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" \
    >> "${PROGRESS}" 2>/dev/null || true
}

if docker image inspect "${TAG}" >/dev/null 2>&1; then
  # Recorded even though nothing was built. "The image was already there" is
  # the answer to "why was this run fast", and a stage that silently never
  # appears cannot answer it.
  progress_event stage_entered '{}'
  progress_event stage_left "{\"elapsed_seconds\": 0, \"note\": \"image ${TAG} was already built\"}"
else
  progress_event stage_entered "{\"tag\": \"${TAG}\"}"
  BUILD_STARTED="${SECONDS}"
  if bash "${REPO}/docker/pilot-controller/build.sh" "${COMMIT}" "${TAG}" "${REPO}"; then
    progress_event stage_left \
      "{\"elapsed_seconds\": $(( SECONDS - BUILD_STARTED )), \"note\": \"built ${TAG}\"}"
  else
    BUILD_STATUS=$?
    progress_event stage_left \
      "{\"elapsed_seconds\": $(( SECONDS - BUILD_STARTED )), \"failed\": \"the controller image build failed\"}"
    exit "${BUILD_STATUS}"
  fi
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

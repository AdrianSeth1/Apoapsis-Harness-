#!/usr/bin/env bash
set -euo pipefail

# Run from Ubuntu-24.04 under WSL2.  The controller image contains only the
# committed runner; this checkout contributes the authorization and its bound
# read-only qualification/evaluation inputs, but nothing executable.
REPO="$(git rev-parse --show-toplevel)"
AUTH="${REPO}/docs/qualification/slice7-crisis-atlas-live-authorization-v1.json"
SEED="${APOAPSIS_CRISIS_ATLAS_SEED:-/home/arya/apoapsis-7p2s/.apoapsis-eval/slice-e-crisis-atlas-seed-2026-07-29}"
EVIDENCE="${1:-/home/arya/apoapsis-live-evidence/crisis-atlas-live-pilot-v2}"

test -f "${AUTH}"
test -d "${SEED}/.git"
test "$(git -C "${SEED}" rev-parse HEAD)" = "197b3610e5720cf36718c548fa19c05fe784a978"
test ! -e "${EVIDENCE}"
mkdir -p "${EVIDENCE}"

IMAGE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["controller_image_id"])' "${AUTH}")"
docker image inspect "${IMAGE_ID}" >/dev/null

docker run --rm --pull never --network host --gpus all \
  --name apoapsis-crisis-atlas-live-pilot \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${AUTH}:/authorization.json:ro" \
  -v "${REPO}/docs:/opt/apoapsis/docs:ro" \
  -v "${SEED}:${SEED}:ro" \
  -v "${EVIDENCE}:${EVIDENCE}:rw" \
  -v /home/arya/llama.cpp:/home/arya/llama.cpp:ro \
  -v /home/arya/models:/home/arya/models:ro \
  "${IMAGE_ID}" -m apoapsis.qualification.live_pilot \
  --repo /opt/apoapsis \
  --authorization /authorization.json \
  --evidence-root "${EVIDENCE}" \
  --seed-repository "${SEED}" \
  --operator-acknowledgement I-AUTHORIZE-SIX-LOCAL-INFERENCE-ARMS

echo "Pilot finished. Send back: ${EVIDENCE}/live-pilot-result.json"

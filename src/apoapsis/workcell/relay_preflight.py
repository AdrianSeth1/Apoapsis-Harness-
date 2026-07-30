"""Prove the whole egress path works before spending a run on it.

The point of this module is that it does not test any *part* of the path. A
relay unit test, a socket connect, and a container that starts all pass
individually while the assembled thing is broken — that is exactly how the
Windows/Docker-Desktop socket problem hides until the first model request.

So the readiness check runs end to end, in the real container:

    container process → 127.0.0.1:port → forwarder → Unix socket
        → controller relay → configured model upstream

in three escalating steps, each spending strictly more than the last:

1. **forwarder liveness** — is anything listening on the loopback port?
2. **health route** — a full round trip to the upstream, zero tokens.
3. **one-token completion** — a real generation with `max_tokens: 1`.

Step 3 is the only one that proves the chat route, the chat template, and the
model are all actually working together, and one token is the cheapest honest
way to establish it.
"""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: Executed inside the container. Uses only the standard library, because the
#: image is minimal and already has Python for the forwarder.
_PROBE_SOURCE = r"""
import json, sys, urllib.error, urllib.request
method, url, payload = sys.argv[1], sys.argv[2], sys.argv[3]
data = payload.encode("utf-8") if payload else None
request = urllib.request.Request(url, data=data, method=method)
if data:
    request.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(request, timeout=float(sys.argv[4])) as response:
        body = response.read(65536).decode("utf-8", "replace")
        print(json.dumps({"status": response.status, "body": body}))
except urllib.error.HTTPError as exc:
    body = exc.read(65536).decode("utf-8", "replace")
    print(json.dumps({"status": exc.code, "body": body}))
except Exception as exc:
    print(json.dumps({"status": 0, "error": f"{type(exc).__name__}: {exc}"}))
"""


class ReadinessStep(StrEnum):
    FORWARDER_LISTENING = "forwarder_listening"
    HEALTH_ROUND_TRIP = "health_round_trip"
    ONE_TOKEN_COMPLETION = "one_token_completion"


class StepStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ReadinessStepResult(StrictModel):
    step: ReadinessStep
    status: StepStatus = StepStatus.NOT_RUN
    http_status: int | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    detail: str = ""


class RelayReadinessReport(StrictModel):
    schema_version: str = "1.0"
    steps: list[ReadinessStepResult] = Field(default_factory=list)
    ready: bool = False
    #: Requests the relay actually saw. If step 3 passed and this is zero, the
    #: CLI reached *something*, and it was not the controller's relay.
    relay_requests_observed: int = Field(default=0, ge=0)
    detail: str = Field(min_length=1)

    def step(self, step: ReadinessStep) -> ReadinessStepResult:
        for item in self.steps:
            if item.step == step:
                return item
        raise KeyError(step)


def build_probe_argv(
    *, method: str, url: str, payload: str, timeout_seconds: float
) -> list[str]:
    """The argv executed inside the container for one readiness probe."""

    return [
        "python3",
        "-c",
        _PROBE_SOURCE,
        method,
        url,
        payload,
        str(timeout_seconds),
    ]


def one_token_payload(model_name: str) -> str:
    """The smallest request that still exercises the real chat path.

    `max_tokens: 1` and a trivial prompt: enough to prove the route, template,
    and model agree, cheap enough to run on every preflight.
    """

    return json.dumps(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
        }
    )


def classify_probe_output(
    step: ReadinessStep, *, stdout: str, exit_code: int | None, duration: float
) -> ReadinessStepResult:
    """Turn one in-container probe's stdout into a step result."""

    if exit_code is None:
        return ReadinessStepResult(
            step=step,
            status=StepStatus.FAILED,
            duration_seconds=duration,
            detail="the probe did not complete inside the container",
        )
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ReadinessStepResult(
            step=step,
            status=StepStatus.FAILED,
            duration_seconds=duration,
            detail=f"the probe produced no usable result: {stdout.strip()[:200]!r}",
        )

    status = payload.get("status")
    if not isinstance(status, int) or status == 0:
        return ReadinessStepResult(
            step=step,
            status=StepStatus.FAILED,
            duration_seconds=duration,
            detail=str(payload.get("error") or "the request did not reach the relay"),
        )
    if status >= 400:
        return ReadinessStepResult(
            step=step,
            status=StepStatus.FAILED,
            http_status=status,
            duration_seconds=duration,
            detail=f"HTTP {status}: {str(payload.get('body'))[:300]}",
        )

    if step == ReadinessStep.ONE_TOKEN_COMPLETION:
        body = payload.get("body") or ""
        try:
            parsed = json.loads(body)
        except ValueError:
            return ReadinessStepResult(
                step=step,
                status=StepStatus.FAILED,
                http_status=status,
                duration_seconds=duration,
                detail="the completion response was not JSON",
            )
        if not parsed.get("choices"):
            return ReadinessStepResult(
                step=step,
                status=StepStatus.FAILED,
                http_status=status,
                duration_seconds=duration,
                detail=(
                    "HTTP 200 with no choices: the path is open but the model "
                    "did not generate"
                ),
            )
    return ReadinessStepResult(
        step=step,
        status=StepStatus.PASSED,
        http_status=status,
        duration_seconds=duration,
    )


def evaluate_readiness(
    results: list[ReadinessStepResult], *, relay_requests_observed: int
) -> RelayReadinessReport:
    """Fail closed, and cross-check against what the relay itself saw.

    The cross-check matters: a step 3 that succeeded while the relay counted
    zero requests means the container reached a model by some path other than
    the controller's socket. That is a containment failure wearing a green
    tick, and it must not read as ready.
    """

    by_step = {item.step: item for item in results}
    complete = [
        by_step.get(step)
        or ReadinessStepResult(
            step=step,
            status=StepStatus.NOT_RUN,
            detail="no result was recorded for this step",
        )
        for step in ReadinessStep
    ]
    failed = [item for item in complete if item.status == StepStatus.FAILED]
    not_run = [item for item in complete if item.status == StepStatus.NOT_RUN]

    if failed:
        detail = (
            f"{len(failed)} readiness step(s) failed: "
            + "; ".join(f"{item.step.value}: {item.detail}" for item in failed)
        )
        ready = False
    elif not_run:
        detail = (
            f"{len(not_run)} readiness step(s) never ran: "
            + ", ".join(item.step.value for item in not_run)
        )
        ready = False
    elif relay_requests_observed <= 0:
        detail = (
            "every readiness step passed but the controller's relay observed no "
            "requests. The container reached a model by some path other than "
            "the relay socket, which is a containment failure, not readiness."
        )
        ready = False
    else:
        detail = (
            "the full path is live: container to loopback forwarder to Unix "
            f"socket to controller relay to model, with {relay_requests_observed} "
            "request(s) observed by the relay"
        )
        ready = True

    return RelayReadinessReport(
        steps=complete,
        ready=ready,
        relay_requests_observed=relay_requests_observed,
        detail=detail,
    )

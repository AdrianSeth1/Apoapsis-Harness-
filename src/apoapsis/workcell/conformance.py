"""Provider and tool-template conformance, run before any quality measurement.

A capable CLI can still perform badly if the OpenAI-compatible adapter, the
`llama-server` chat template, or the tool parser disagrees subtly with what the
CLI expects. If that happens, every downstream number measures the adapter and
gets written down as the model's reasoning.

Hence the standing rule this module exists to enforce:

> A malformed tool envelope is an adapter defect until the conformance suite
> proves otherwise.

The Crisis Atlas sliced arm is the cautionary case. Two of its nineteen calls
produced artifacts "truncated/invalid for the agent protocol". That was later
established to be the 8,192-token output cap, but nothing in the harness could
have distinguished it at the time from a chat template mangling a tool call.

Every check is declared here with an explicit `ConformanceStatus`, and
`evaluate_conformance` treats `NOT_RUN` as failure for the same reason
containment does: the suite that quietly shrank is the one that lets a defect
through.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from apoapsis.models.ceilings import CeilingStopReason
from apoapsis.specification.schema import StrictModel


class ConformanceCheck(StrEnum):
    """The checks the handoff requires before agent quality is measured."""

    #: system, user, assistant, tool-call, and tool-result roles round-trip.
    ROLE_ROUND_TRIP = "role_round_trip"
    #: A single tool call preserves its name and JSON arguments exactly.
    SINGLE_TOOL_CALL = "single_tool_call"
    #: Parallel tool calls preserve every name and argument object, in order.
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    #: Multiline Unicode file content is not escaped, truncated, or
    #: double-encoded on the way through the template.
    MULTILINE_UNICODE_INTEGRITY = "multiline_unicode_integrity"
    #: Thinking blocks are either supported or stripped exactly once. Stripping
    #: twice silently eats real content that happens to look like a tag.
    THINKING_BLOCK_HANDLING = "thinking_block_handling"
    #: Stop reasons distinguish normal completion, tool call, context limit,
    #: output limit, cancellation, and provider error.
    STOP_REASON_FIDELITY = "stop_reason_fidelity"
    #: Usage counts and the maximum-output setting survive the round trip.
    USAGE_ACCOUNTING = "usage_accounting"
    #: A replayed or retried response cannot execute the same mutating tool
    #: twice.
    REPLAY_NON_IDEMPOTENCE_GUARD = "replay_non_idempotence_guard"
    #: The CLI's declared context and output limits match the server profile.
    DECLARED_LIMITS_MATCH_SERVER = "declared_limits_match_server"


class ConformanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: Ran, but did not establish the property. Fails the gate.
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"


#: The six outcomes a provider must be able to tell apart. Five map to a
#: ceiling condition; `NORMAL_COMPLETION` and `TOOL_CALL` are ordinary
#: successes, and `CANCELLED` is neither a ceiling nor a model failure.
class ObservedStopReason(StrEnum):
    NORMAL_COMPLETION = "normal_completion"
    TOOL_CALL = "tool_call"
    CONTEXT_LIMIT = "context_limit"
    OUTPUT_LIMIT = "output_limit"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"


#: How an observed stop reason maps onto the first-class ceiling conditions.
#: `None` means the outcome is not a ceiling condition at all.
STOP_REASON_CEILING_MAP: dict[ObservedStopReason, CeilingStopReason | None] = {
    ObservedStopReason.NORMAL_COMPLETION: None,
    ObservedStopReason.TOOL_CALL: None,
    ObservedStopReason.CONTEXT_LIMIT: CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
    ObservedStopReason.OUTPUT_LIMIT: CeilingStopReason.OUTPUT_CEILING_TRUNCATION,
    ObservedStopReason.CANCELLED: None,
    ObservedStopReason.PROVIDER_ERROR: None,
}


class CheckResult(StrictModel):
    check: ConformanceCheck
    status: ConformanceStatus = ConformanceStatus.NOT_RUN
    detail: str = ""
    #: Raw evidence pointer, never the raw payload itself.
    audit_artifact: str | None = None


class ConformanceReport(StrictModel):
    schema_version: str = "1.0"
    workcell_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    results: list[CheckResult] = Field(default_factory=list)
    conformant: bool = False
    failed: list[ConformanceCheck] = Field(default_factory=list)
    unproven: list[ConformanceCheck] = Field(default_factory=list)
    detail: str = ""


def check_stop_reason_fidelity(
    observed: dict[ObservedStopReason, str],
) -> CheckResult:
    """Every one of the six outcomes must map to a *distinct* provider signal.

    This is the check that would have paid for itself on Crisis Atlas. The
    control's context exhaustion and the sliced arm's output-cap truncation
    both arrived as `finish_reason="length"`; only the token counts told them
    apart. If a provider cannot distinguish them at all, the ceiling classifier
    is guessing, and the efficiency gate is measuring noise.
    """

    missing = [reason for reason in ObservedStopReason if reason not in observed]
    if missing:
        return CheckResult(
            check=ConformanceCheck.STOP_REASON_FIDELITY,
            status=ConformanceStatus.NOT_RUN,
            detail=(
                "no signal was captured for: "
                + ", ".join(sorted(item.value for item in missing))
            ),
        )

    collisions: dict[str, list[str]] = {}
    for reason, signal in observed.items():
        collisions.setdefault(signal, []).append(reason.value)
    ambiguous = {
        signal: sorted(names)
        for signal, names in collisions.items()
        if len(names) > 1
    }
    # `length` covering both limits is the known, tolerable case: the token
    # counts disambiguate it and `classify_ceiling_stop_reason` does exactly
    # that. Anything else collapsing is a real loss of information.
    tolerated = {ObservedStopReason.CONTEXT_LIMIT.value, ObservedStopReason.OUTPUT_LIMIT.value}
    unacceptable = {
        signal: names for signal, names in ambiguous.items() if set(names) != tolerated
    }
    if unacceptable:
        return CheckResult(
            check=ConformanceCheck.STOP_REASON_FIDELITY,
            status=ConformanceStatus.FAILED,
            detail=(
                "these outcomes are indistinguishable at the provider boundary: "
                + "; ".join(
                    f"{signal!r} -> {', '.join(names)}"
                    for signal, names in sorted(unacceptable.items())
                )
            ),
        )
    if ambiguous:
        return CheckResult(
            check=ConformanceCheck.STOP_REASON_FIDELITY,
            status=ConformanceStatus.PASSED,
            detail=(
                "context and output limits share the 'length' signal, which is "
                "expected; token counts disambiguate them and the ceiling "
                "classifier uses exactly that"
            ),
        )
    return CheckResult(
        check=ConformanceCheck.STOP_REASON_FIDELITY,
        status=ConformanceStatus.PASSED,
        detail="all six outcomes carry distinct provider signals",
    )


def check_multiline_unicode_integrity(*, sent: str, received: str) -> CheckResult:
    """The content must survive byte-for-byte.

    Named separately from a generic equality check because the failure modes
    are specific and each one silently corrupts file writes: escaping that
    turns a newline into a literal `\\n`, double-encoding that turns `é` into
    `Ã©`, and truncation that loses the tail.
    """

    if sent == received:
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.PASSED,
            detail=f"{len(sent)} characters round-tripped unchanged",
        )
    if received and sent.startswith(received):
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.FAILED,
            detail=(
                f"content was truncated from {len(sent)} to {len(received)} "
                "characters; a file tool would write a partial file"
            ),
        )
    if "\\n" in received and "\\n" not in sent:
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.FAILED,
            detail="newlines came back escaped as literal backslash-n",
        )
    try:
        if received == sent.encode("utf-8").decode("latin-1"):
            return CheckResult(
                check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
                status=ConformanceStatus.FAILED,
                detail="content was double-encoded (UTF-8 bytes read as Latin-1)",
            )
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return CheckResult(
        check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
        status=ConformanceStatus.FAILED,
        detail=(
            f"content changed in transit: sent {len(sent)} characters, received "
            f"{len(received)}"
        ),
    )


def check_thinking_block_handling(
    *, supported: bool, stripped_once: str, stripped_twice: str
) -> CheckResult:
    """Stripping must be idempotent, or real content disappears.

    A stripper applied twice will eat any literal `<think>` the model wrote
    inside a code block — which is exactly the sort of thing a coding agent
    writes when the task is about parsing tags.
    """

    if supported:
        return CheckResult(
            check=ConformanceCheck.THINKING_BLOCK_HANDLING,
            status=ConformanceStatus.PASSED,
            detail="thinking blocks are natively supported and preserved",
        )
    if stripped_once == stripped_twice:
        return CheckResult(
            check=ConformanceCheck.THINKING_BLOCK_HANDLING,
            status=ConformanceStatus.PASSED,
            detail="stripping is idempotent; a second pass changes nothing",
        )
    return CheckResult(
        check=ConformanceCheck.THINKING_BLOCK_HANDLING,
        status=ConformanceStatus.FAILED,
        detail=(
            "stripping is not idempotent, so a retry would remove content the "
            "first pass kept"
        ),
    )


def check_declared_limits_match_server(
    *,
    cli_context_limit: int,
    cli_max_output: int,
    server_context_limit: int,
    server_max_output: int,
) -> CheckResult:
    """The CLI's belief about the window must match the server's actual profile.

    A CLI that thinks the window is larger than it is will compact too late and
    walk into the exact HTTP 500 the unrestricted control hit. A CLI that
    thinks it is smaller wastes context it was paid for.
    """

    problems: list[str] = []
    if cli_context_limit != server_context_limit:
        problems.append(
            f"context limit: CLI declares {cli_context_limit:,}, server reports "
            f"{server_context_limit:,}"
        )
    if cli_max_output != server_max_output:
        problems.append(
            f"max output: CLI declares {cli_max_output:,}, server reports "
            f"{server_max_output:,}"
        )
    if problems:
        return CheckResult(
            check=ConformanceCheck.DECLARED_LIMITS_MATCH_SERVER,
            status=ConformanceStatus.FAILED,
            detail="; ".join(problems),
        )
    return CheckResult(
        check=ConformanceCheck.DECLARED_LIMITS_MATCH_SERVER,
        status=ConformanceStatus.PASSED,
        detail=(
            f"both report a {server_context_limit:,}-token window and a "
            f"{server_max_output:,}-token output cap"
        ),
    )


def check_replay_non_idempotence_guard(
    *, mutating_tool_call_id: str, executed_call_ids: list[str]
) -> CheckResult:
    """A retried response must not execute the same mutating tool twice.

    Retries happen — on a provider error, on a truncated response, on a
    reconnect. If the harness replays a response that already ran `write_file`,
    the second execution is invisible in the transcript and shows up only as a
    mysterious delta.
    """

    executions = executed_call_ids.count(mutating_tool_call_id)
    if executions == 0:
        return CheckResult(
            check=ConformanceCheck.REPLAY_NON_IDEMPOTENCE_GUARD,
            status=ConformanceStatus.INCONCLUSIVE,
            detail=(
                f"the mutating call {mutating_tool_call_id!r} never executed, so "
                "replay protection was not exercised"
            ),
        )
    if executions == 1:
        return CheckResult(
            check=ConformanceCheck.REPLAY_NON_IDEMPOTENCE_GUARD,
            status=ConformanceStatus.PASSED,
            detail="the replayed response did not re-execute the mutating tool",
        )
    return CheckResult(
        check=ConformanceCheck.REPLAY_NON_IDEMPOTENCE_GUARD,
        status=ConformanceStatus.FAILED,
        detail=(
            f"the mutating call {mutating_tool_call_id!r} executed {executions} "
            "times; a retry duplicated a side effect"
        ),
    )


def evaluate_conformance(
    results: list[CheckResult], *, workcell_manifest_digest: str
) -> ConformanceReport:
    """Fail closed across every declared check.

    A check absent from `results` is `NOT_RUN`, and `NOT_RUN` is not a pass.
    """

    by_check = {item.check: item for item in results}
    complete = [
        by_check.get(check)
        or CheckResult(
            check=check,
            status=ConformanceStatus.NOT_RUN,
            detail="no result was recorded for this check",
        )
        for check in ConformanceCheck
    ]
    failed = [
        item.check for item in complete if item.status == ConformanceStatus.FAILED
    ]
    unproven = [
        item.check
        for item in complete
        if item.status in {ConformanceStatus.INCONCLUSIVE, ConformanceStatus.NOT_RUN}
    ]

    if failed:
        detail = (
            f"{len(failed)} conformance failure(s): "
            + ", ".join(item.value for item in failed)
            + ". Treat these as adapter defects, not model behaviour, and do "
            "not measure agent quality until they are fixed."
        )
    elif unproven:
        detail = (
            f"{len(unproven)} check(s) did not establish conformance: "
            + ", ".join(item.value for item in unproven)
        )
    else:
        detail = f"all {len(complete)} conformance checks passed"

    return ConformanceReport(
        workcell_manifest_digest=workcell_manifest_digest,
        results=complete,
        conformant=not failed and not unproven,
        failed=failed,
        unproven=unproven,
        detail=detail,
    )

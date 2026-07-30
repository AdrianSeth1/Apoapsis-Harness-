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


def check_envelope_integrity(
    *, sent_bytes: bytes, received_bytes: bytes
) -> CheckResult:
    """The byte-level form of `check_multiline_unicode_integrity`.

    ADR 0078. The evidence is the payload as it was captured off the wire on
    the way in, and the payload as it was parsed out of the response bytes on
    the way back -- with a deterministic echo provider in between, so nothing
    in the path is entitled to change it. That is what makes a difference here
    attributable: under the previous design a difference could equally well
    have been a model retyping a quotation mark, and in Slice 2B it was.

    Bytes, not `str`, because two of the three named failure modes are
    invisible after decoding. A UTF-8 payload re-read as Latin-1 and re-encoded
    is a different byte string that compares equal to nothing useful once it
    has been normalised into Python text, and a truncation that lands mid
    code-point is a decode error rather than a shorter string.
    """

    if sent_bytes == received_bytes:
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.PASSED,
            detail=(
                f"{len(sent_bytes)} bytes round-tripped byte-for-byte through "
                "the relay, the forwarder, and the tool-call envelope"
            ),
        )
    if not received_bytes:
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.FAILED,
            detail=(
                f"{len(sent_bytes)} bytes were sent and nothing came back; the "
                "envelope did not carry the content at all"
            ),
        )
    if sent_bytes.startswith(received_bytes):
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.FAILED,
            detail=(
                f"content was truncated from {len(sent_bytes)} to "
                f"{len(received_bytes)} bytes; a file tool would write a "
                "partial file"
            ),
        )
    # Below this point the difference is a substitution rather than a length
    # change, so the specific corruptions are worth naming individually.
    sent_text = sent_bytes.decode("utf-8", "replace")
    received_text = received_bytes.decode("utf-8", "replace")
    if "\\n" in received_text and "\\n" not in sent_text:
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.FAILED,
            detail="newlines came back escaped as literal backslash-n",
        )
    if received_bytes == sent_text.encode("utf-8").decode("latin-1", "replace").encode(
        "utf-8", "replace"
    ):
        return CheckResult(
            check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
            status=ConformanceStatus.FAILED,
            detail="content was double-encoded (UTF-8 bytes read as Latin-1)",
        )
    return CheckResult(
        check=ConformanceCheck.MULTILINE_UNICODE_INTEGRITY,
        status=ConformanceStatus.FAILED,
        detail=(
            f"content changed in transit: sent {len(sent_bytes)} bytes, "
            f"received {len(received_bytes)}"
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


class ObservedToolCall(StrictModel):
    """One tool call as it came back off the wire.

    `arguments` is kept as the raw string the provider emitted *and* as the
    parsed object, because the two failure modes are different: a provider that
    returns valid JSON with the wrong values is a template bug, and one that
    returns unparseable text is a tool-parser bug. Collapsing them into a single
    "arguments" field would make the distinction unreportable.
    """

    call_id: str = ""
    name: str = Field(min_length=1)
    raw_arguments: str = ""
    arguments: dict | None = None
    parse_error: str | None = None


def check_role_round_trip(
    *, roles_sent: list[str], status: int, assistant_text: str
) -> CheckResult:
    """Every role the CLI uses must survive the template without an error.

    The check is deliberately weak about *content* and strict about
    *acceptance*: a template that cannot render a `tool` role at all fails with
    a 4xx or 5xx, and that is the defect worth catching here. Judging the
    assistant's prose would be measuring the model, which this suite must not do.
    """

    required = ["system", "user", "assistant", "tool"]
    missing = [role for role in required if role not in roles_sent]
    if missing:
        return CheckResult(
            check=ConformanceCheck.ROLE_ROUND_TRIP,
            status=ConformanceStatus.NOT_RUN,
            detail=(
                "the exchange did not include every role the CLI uses; missing: "
                + ", ".join(missing)
            ),
        )
    if status != 200:
        return CheckResult(
            check=ConformanceCheck.ROLE_ROUND_TRIP,
            status=ConformanceStatus.FAILED,
            detail=(
                f"a system/user/assistant/tool-call/tool-result exchange was "
                f"rejected with HTTP {status}; the chat template cannot render "
                "the roles the CLI actually sends"
            ),
        )
    if not assistant_text.strip():
        return CheckResult(
            check=ConformanceCheck.ROLE_ROUND_TRIP,
            status=ConformanceStatus.INCONCLUSIVE,
            detail=(
                "the exchange was accepted but the assistant turn was empty, so "
                "nothing establishes that the tool result was actually read"
            ),
        )
    return CheckResult(
        check=ConformanceCheck.ROLE_ROUND_TRIP,
        status=ConformanceStatus.PASSED,
        detail=(
            "system, user, assistant, tool-call, and tool-result roles were "
            f"accepted and continued ({len(assistant_text)} characters)"
        ),
    )


def check_single_tool_call(
    *,
    expected_name: str,
    expected_arguments: dict,
    observed: list[ObservedToolCall],
) -> CheckResult:
    """One tool call must preserve its name and its JSON arguments exactly."""

    if not observed:
        return CheckResult(
            check=ConformanceCheck.SINGLE_TOOL_CALL,
            status=ConformanceStatus.INCONCLUSIVE,
            detail=(
                "the model returned no tool call, so the envelope was never "
                "exercised; this measures the model, not the adapter"
            ),
        )
    call = observed[0]
    if call.parse_error is not None:
        return CheckResult(
            check=ConformanceCheck.SINGLE_TOOL_CALL,
            status=ConformanceStatus.FAILED,
            detail=(
                f"tool call arguments did not parse as JSON ({call.parse_error}); "
                "a malformed tool envelope is an adapter defect"
            ),
        )
    if call.name != expected_name:
        return CheckResult(
            check=ConformanceCheck.SINGLE_TOOL_CALL,
            status=ConformanceStatus.FAILED,
            detail=(
                f"the tool name did not survive: sent schema {expected_name!r}, "
                f"received {call.name!r}"
            ),
        )
    arguments = call.arguments or {}
    wrong = {
        key: (value, arguments.get(key))
        for key, value in expected_arguments.items()
        if arguments.get(key) != value
    }
    if wrong:
        return CheckResult(
            check=ConformanceCheck.SINGLE_TOOL_CALL,
            status=ConformanceStatus.FAILED,
            detail=(
                "argument values did not round-trip: "
                + "; ".join(
                    f"{key}: expected {want!r}, received {got!r}"
                    for key, (want, got) in sorted(wrong.items())
                )
            ),
        )
    return CheckResult(
        check=ConformanceCheck.SINGLE_TOOL_CALL,
        status=ConformanceStatus.PASSED,
        detail=(
            f"tool {expected_name!r} round-tripped with "
            f"{len(expected_arguments)} argument(s) intact"
        ),
    )


def check_parallel_tool_calls(
    *,
    expected: list[tuple[str, dict]],
    observed: list[ObservedToolCall],
    server_declares_support: bool = True,
) -> CheckResult:
    """Parallel calls must preserve every name and argument object, in order.

    Order is part of the property because a tool loop that reorders calls will
    execute a write before the read it depended on.
    """

    if not server_declares_support:
        return CheckResult(
            check=ConformanceCheck.PARALLEL_TOOL_CALLS,
            status=ConformanceStatus.FAILED,
            detail=(
                "the chat template does not declare parallel tool-call support, "
                "so the CLI's batched read/search calls cannot round-trip"
            ),
        )
    if len(observed) < 2:
        return CheckResult(
            check=ConformanceCheck.PARALLEL_TOOL_CALLS,
            status=ConformanceStatus.INCONCLUSIVE,
            detail=(
                f"only {len(observed)} tool call(s) came back, so parallel "
                "dispatch was never exercised"
            ),
        )
    broken = [call.name for call in observed if call.parse_error is not None]
    if broken:
        return CheckResult(
            check=ConformanceCheck.PARALLEL_TOOL_CALLS,
            status=ConformanceStatus.FAILED,
            detail=(
                "these parallel calls had unparseable arguments: "
                + ", ".join(sorted(broken))
            ),
        )
    observed_names = [call.name for call in observed]
    expected_names = [name for name, _arguments in expected]
    # Compared as an ordered prefix: the model may add calls we did not ask for,
    # but the ones we asked for must arrive, in the order they were requested.
    if observed_names[: len(expected_names)] != expected_names:
        return CheckResult(
            check=ConformanceCheck.PARALLEL_TOOL_CALLS,
            status=ConformanceStatus.FAILED,
            detail=(
                f"parallel call order or naming changed: expected "
                f"{expected_names}, received {observed_names}"
            ),
        )
    ids = [call.call_id for call in observed if call.call_id]
    if len(set(ids)) != len(ids):
        return CheckResult(
            check=ConformanceCheck.PARALLEL_TOOL_CALLS,
            status=ConformanceStatus.FAILED,
            detail=(
                "two parallel calls share a call id, so their results cannot be "
                "matched back to the call that produced them"
            ),
        )
    for index, (name, arguments) in enumerate(expected):
        received = observed[index].arguments or {}
        wrong = {
            key: (value, received.get(key))
            for key, value in arguments.items()
            if received.get(key) != value
        }
        if wrong:
            return CheckResult(
                check=ConformanceCheck.PARALLEL_TOOL_CALLS,
                status=ConformanceStatus.FAILED,
                detail=(
                    f"call {index} ({name}) lost argument values: "
                    + "; ".join(
                        f"{key}: expected {want!r}, received {got!r}"
                        for key, (want, got) in sorted(wrong.items())
                    )
                ),
            )
    return CheckResult(
        check=ConformanceCheck.PARALLEL_TOOL_CALLS,
        status=ConformanceStatus.PASSED,
        detail=(
            f"{len(observed)} parallel call(s) preserved every name, argument "
            "object, and distinct call id, in order"
        ),
    )


def check_usage_accounting(
    *,
    requested_max_output: int,
    usage: dict | None,
    finish_reason: str | None,
) -> CheckResult:
    """Usage counts must be reported, and the output cap must be obeyed.

    Both halves matter. Missing usage means the efficiency gate is measuring
    nothing, and an output cap the server silently ignores means the harness
    cannot tell a truncation from a completion.
    """

    if not usage:
        return CheckResult(
            check=ConformanceCheck.USAGE_ACCOUNTING,
            status=ConformanceStatus.FAILED,
            detail=(
                "the response carried no usage object; token accounting and the "
                "efficiency gate would both be unmeasurable"
            ),
        )
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    missing = [
        name
        for name, value in (
            ("prompt_tokens", prompt_tokens),
            ("completion_tokens", completion_tokens),
        )
        if not isinstance(value, int)
    ]
    if missing:
        return CheckResult(
            check=ConformanceCheck.USAGE_ACCOUNTING,
            status=ConformanceStatus.FAILED,
            detail="usage is missing integer field(s): " + ", ".join(missing),
        )
    assert isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
    if prompt_tokens <= 0:
        return CheckResult(
            check=ConformanceCheck.USAGE_ACCOUNTING,
            status=ConformanceStatus.FAILED,
            detail=(
                f"the server reported {prompt_tokens} prompt tokens for a "
                "non-empty prompt, so input accounting is not trustworthy"
            ),
        )
    if completion_tokens > requested_max_output:
        return CheckResult(
            check=ConformanceCheck.USAGE_ACCOUNTING,
            status=ConformanceStatus.FAILED,
            detail=(
                f"the response generated {completion_tokens} tokens against a "
                f"requested cap of {requested_max_output}; the maximum-output "
                "setting did not survive the round trip"
            ),
        )
    if completion_tokens == requested_max_output and finish_reason != "length":
        return CheckResult(
            check=ConformanceCheck.USAGE_ACCOUNTING,
            status=ConformanceStatus.FAILED,
            detail=(
                f"generation stopped exactly at the {requested_max_output}-token "
                f"cap but reported finish reason {finish_reason!r} rather than "
                "'length', so a truncation would be recorded as a completion"
            ),
        )
    return CheckResult(
        check=ConformanceCheck.USAGE_ACCOUNTING,
        status=ConformanceStatus.PASSED,
        detail=(
            f"{prompt_tokens} prompt and {completion_tokens} completion token(s) "
            f"were reported against a {requested_max_output}-token cap, and the "
            f"finish reason {finish_reason!r} is consistent with them"
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

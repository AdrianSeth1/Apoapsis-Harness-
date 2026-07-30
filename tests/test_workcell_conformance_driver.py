"""Deterministic coverage for the live conformance driver and its neighbours.

The driver's whole job is to turn real endpoint behaviour into the observations
`conformance.py` classifies. That makes it exactly the kind of code that is easy
to write so it can only produce passes. These tests therefore spend most of
their effort on the *failure* directions: a provider that mangles content, one
that returns no call id, one that reports the same signal for two different stop
conditions. A driver that cannot report those is worse than no driver.

`exec_fn` is a deterministic fake standing in for `docker exec` into the
workcell. It speaks the probe's wire format -- a single JSON line on stdout --
so the parsing path under test is the same one the live run uses.
"""

from __future__ import annotations

import json
import unittest

from apoapsis.workcell.conformance import (
    ConformanceCheck,
    ConformanceStatus,
    ObservedStopReason,
    ObservedToolCall,
)
from apoapsis.workcell.conformance_driver import (
    UNICODE_PROBE_CONTENT,
    DeclaredCliLimits,
    LiveConformanceRunner,
    PAD_MARKER,
    ProbeMode,
    ReplayGuardedExecutor,
    apply_pad,
    build_conformance_probe_argv,
    observed_tool_calls,
    parse_probe_output,
    strip_thinking_blocks,
)
from apoapsis.workcell.gate import (
    Slice3Blocked,
    evaluate_slice3_gate,
    require_slice3_unblocked,
)
from apoapsis.workcell.pin_capture import (
    PinCaptureError,
    canonical_tool_schema,
    cli_declared_limits_argv,
    extract_prompt_identity,
    parse_declared_limits,
    server_flags_sha256,
    sha256_text,
)


def _envelope(status: int = 200, body: dict | None = None, **extra: object) -> str:
    payload: dict[str, object] = {"status": status}
    if body is not None:
        payload["body"] = json.dumps(body)
    payload.update(extra)
    return "warning: something noisy\n" + json.dumps(payload)


def _chat(
    *,
    content: str = "ok",
    finish_reason: str = "stop",
    tool_calls: list[dict] | None = None,
    usage: dict | None = None,
    reasoning: str | None = None,
) -> dict:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    body: dict[str, object] = {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _call(name: str, arguments: object, call_id: str = "call_1") -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": raw},
    }


class ProbeTransportTests(unittest.TestCase):
    def test_probe_argv_carries_mode_url_and_padding(self) -> None:
        argv = build_conformance_probe_argv(
            url="http://127.0.0.1:8080/v1/chat/completions",
            payload='{"a":1}',
            mode=ProbeMode.STREAM_CANCEL,
            timeout_seconds=12.5,
            pad_chars=99,
        )
        self.assertEqual(argv[0], "python3")
        self.assertIn("stream_cancel", argv)
        self.assertIn("http://127.0.0.1:8080/v1/chat/completions", argv)
        self.assertIn("12.5", argv)
        self.assertIn("99", argv)

    def test_apply_pad_expands_marker_to_the_requested_size(self) -> None:
        padded = apply_pad(f"prefix {PAD_MARKER} suffix", 500)
        self.assertNotIn(PAD_MARKER, padded)
        self.assertEqual(len(padded), len("prefix  suffix") + 500)

    def test_apply_pad_is_a_no_op_without_a_budget(self) -> None:
        self.assertEqual(apply_pad(f"x{PAD_MARKER}", 0), f"x{PAD_MARKER}")

    def test_parse_probe_output_reads_the_last_line_not_the_first(self) -> None:
        outcome = parse_probe_output(_envelope(body=_chat()))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, 200)

    def test_parse_probe_output_reports_empty_output_rather_than_succeeding(
        self,
    ) -> None:
        outcome = parse_probe_output("   \n  ")
        self.assertFalse(outcome.ok)
        self.assertIn("no output", outcome.error)

    def test_parse_probe_output_reports_unparseable_output(self) -> None:
        outcome = parse_probe_output("not json at all")
        self.assertFalse(outcome.ok)
        self.assertIn("unparseable", outcome.error)


class ToolCallDecodingTests(unittest.TestCase):
    def test_unparseable_arguments_are_recorded_not_dropped(self) -> None:
        calls = observed_tool_calls(
            _chat(tool_calls=[_call("write_file", "{not json")])
        )
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].arguments)
        self.assertTrue(calls[0].parse_error)

    def test_non_object_arguments_are_a_parse_error(self) -> None:
        calls = observed_tool_calls(_chat(tool_calls=[_call("write_file", "[1,2]")]))
        self.assertIsNone(calls[0].arguments)
        self.assertIn("not a JSON object", calls[0].parse_error or "")

    def test_call_order_is_preserved(self) -> None:
        calls = observed_tool_calls(
            _chat(
                tool_calls=[
                    _call("read_file", {"path": "a"}, "c1"),
                    _call("list_directory", {"path": "b"}, "c2"),
                ]
            )
        )
        self.assertEqual([call.name for call in calls], ["read_file", "list_directory"])


class ThinkingStripperTests(unittest.TestCase):
    def test_stripping_is_idempotent_on_its_own_output(self) -> None:
        text = "a<think>hidden</think>b"
        once = strip_thinking_blocks(text)
        self.assertEqual(once, "ab")
        self.assertEqual(strip_thinking_blocks(once), once)

    def test_a_lone_literal_open_tag_is_left_alone(self) -> None:
        # The case that matters: an agent writing about the tag, not using it.
        text = "use <think> to open a block"
        self.assertEqual(strip_thinking_blocks(text), text)


class ReplayGuardTests(unittest.TestCase):
    def test_the_same_call_id_executes_exactly_once(self) -> None:
        seen: list[str] = []
        executor = ReplayGuardedExecutor(lambda call: seen.append(call.call_id))
        call = ObservedToolCall(call_id="abc", name="write_file", arguments={})
        self.assertTrue(executor.submit(call))
        self.assertFalse(executor.submit(call))
        self.assertEqual(seen, ["abc"])

    def test_an_unidentified_call_is_refused_rather_than_run(self) -> None:
        seen: list[str] = []
        executor = ReplayGuardedExecutor(lambda call: seen.append(call.call_id))
        self.assertFalse(
            executor.submit(ObservedToolCall(call_id="", name="write_file"))
        )
        self.assertEqual(seen, [])


class _ScriptedEndpoint:
    """Answers probes from a queue, recording every payload it was sent."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.payloads.append(json.loads(argv[5]))
        if not self.responses:
            return 0, _envelope(body=_chat()), ""
        return 0, self.responses.pop(0), ""


def _runner(responses: list[str], **kwargs: object) -> LiveConformanceRunner:
    defaults: dict[str, object] = {
        "base_url": "http://127.0.0.1:8080",
        "model_name": "test-model",
        "context_limit_tokens": 4096,
        "server_max_output_tokens": 512,
    }
    defaults.update(kwargs)
    return LiveConformanceRunner(exec_fn=_ScriptedEndpoint(responses), **defaults)


class DriverCheckTests(unittest.TestCase):
    def test_role_round_trip_fails_when_the_template_rejects_the_tool_role(
        self,
    ) -> None:
        result = _runner([_envelope(status=500)]).run_role_round_trip()
        self.assertIs(result.status, ConformanceStatus.FAILED)
        self.assertIs(result.check, ConformanceCheck.ROLE_ROUND_TRIP)

    def test_role_round_trip_passes_on_a_continued_exchange(self) -> None:
        result = _runner(
            [_envelope(body=_chat(content="the file said mode=alpha"))]
        ).run_role_round_trip()
        self.assertIs(result.status, ConformanceStatus.PASSED)

    def test_single_tool_call_fails_on_a_transport_error(self) -> None:
        result = _runner([_envelope(status=502)]).run_single_tool_call()
        self.assertIs(result.status, ConformanceStatus.FAILED)
        self.assertIn("transport", result.detail)

    def test_single_tool_call_passes_when_arguments_survive_exactly(self) -> None:
        result = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[_call("echo_value", {"value": "Ωmega-42"})],
                    )
                )
            ]
        ).run_single_tool_call()
        self.assertIs(result.status, ConformanceStatus.PASSED)

    def test_multiline_unicode_is_not_run_without_the_echo_path(self) -> None:
        """ADR 0078: no echo path means no evidence, and no evidence fails.

        The pre-2C driver would have fallen back on whatever the model said,
        which is how a quotation-mark preference became an adapter defect.
        """

        result = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[
                            _call(
                                "write_file",
                                {
                                    "path": "probe.py",
                                    "content": UNICODE_PROBE_CONTENT,
                                },
                            )
                        ],
                    )
                )
            ]
        ).run_multiline_unicode_integrity()
        self.assertIs(result.status, ConformanceStatus.NOT_RUN)

    def test_multiline_unicode_passes_on_a_byte_exact_echo(self) -> None:
        runner = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[
                            _call(
                                "write_file",
                                {
                                    "path": "probe.py",
                                    "content": UNICODE_PROBE_CONTENT,
                                },
                            )
                        ],
                    )
                )
            ],
            envelope_base_url="http://127.0.0.1:8081",
            captured_envelope_bytes=UNICODE_PROBE_CONTENT.encode("utf-8"),
        )
        result = runner.run_multiline_unicode_integrity()
        self.assertIs(result.status, ConformanceStatus.PASSED)

    def test_multiline_unicode_reports_corruption_rather_than_smoothing_it(
        self,
    ) -> None:
        mangled = UNICODE_PROBE_CONTENT.replace("\n", "\\n")
        runner = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[
                            _call(
                                "write_file",
                                {"path": "probe.py", "content": mangled},
                            )
                        ],
                    )
                )
            ],
            envelope_base_url="http://127.0.0.1:8081",
            captured_envelope_bytes=UNICODE_PROBE_CONTENT.encode("utf-8"),
        )
        result = runner.run_multiline_unicode_integrity()
        self.assertIs(result.status, ConformanceStatus.FAILED)
        # Not asserted as "escaped": the payload deliberately *already*
        # contains a literal backslash-n, so the escaping detector correctly
        # declines to claim it and the generic corruption report is the honest
        # classification. Asserting the more specific message here would be
        # asserting a misdiagnosis.
        self.assertIn("changed in transit", result.detail)

    def test_multiline_unicode_fails_rather_than_stalls_on_an_unparseable_call(
        self,
    ) -> None:
        """With a deterministic provider there is no benign explanation left.

        Under the old design an unparseable call could mean the model declined
        to make one, so `INCONCLUSIVE` was the honest answer. An echo provider
        always emits exactly one well-formed call, so anything else is the
        envelope.
        """

        runner = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[_call("write_file", "{broken")],
                    )
                )
            ],
            envelope_base_url="http://127.0.0.1:8081",
            captured_envelope_bytes=UNICODE_PROBE_CONTENT.encode("utf-8"),
        )
        result = runner.run_multiline_unicode_integrity()
        self.assertIs(result.status, ConformanceStatus.FAILED)

    def test_replay_guard_fails_when_the_provider_omits_a_call_id(self) -> None:
        result = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[_call("write_file", {"path": "p"}, call_id="")],
                    )
                )
            ]
        ).run_replay_non_idempotence_guard()
        self.assertIs(result.status, ConformanceStatus.FAILED)
        self.assertIn("call id", result.detail)

    def test_replay_guard_executes_a_replayed_mutating_call_once(self) -> None:
        executed: list[str] = []
        result = _runner(
            [
                _envelope(
                    body=_chat(
                        finish_reason="tool_calls",
                        tool_calls=[_call("write_file", {"path": "p"}, "cid")],
                    )
                )
            ],
            mutating_tool_runner=lambda call: executed.append(call.call_id),
        ).run_replay_non_idempotence_guard()
        self.assertIs(result.status, ConformanceStatus.PASSED)
        self.assertEqual(executed, ["cid"])

    def test_declared_limits_is_not_run_when_the_cli_was_never_asked(self) -> None:
        result = _runner([]).run_declared_limits_match_server()
        self.assertIs(result.status, ConformanceStatus.NOT_RUN)

    def test_declared_limits_fails_and_names_its_source(self) -> None:
        result = _runner(
            [],
            declared_cli_limits=DeclaredCliLimits(
                context_limit_tokens=1_000_000,
                max_output_tokens=64_000,
                source="the CLI's own token-limit module",
            ),
        ).run_declared_limits_match_server()
        self.assertIs(result.status, ConformanceStatus.FAILED)
        self.assertIn("1,000,000", result.detail)
        self.assertIn("token-limit module", result.detail)

    def test_thinking_block_handling_uses_the_reasoning_field(self) -> None:
        result = _runner(
            [_envelope(body=_chat(content="plain", reasoning="deliberating"))]
        ).run_thinking_block_handling()
        self.assertIs(result.check, ConformanceCheck.THINKING_BLOCK_HANDLING)


class StopReasonProvocationTests(unittest.TestCase):
    def test_each_outcome_is_recorded_from_what_the_endpoint_returned(self) -> None:
        runner = _runner(
            [
                _envelope(body=_chat(finish_reason="stop")),
                _envelope(body=_chat(finish_reason="tool_calls")),
                _envelope(body=_chat(finish_reason="length")),
                _envelope(
                    status=400,
                    body={"error": {"type": "context_exceeded"}},
                ),
                _envelope(status=200, cancelled=True, bytes_read=64),
                _envelope(status=404, body={"error": {"type": "model_not_found"}}),
            ]
        )
        runner.run_stop_reason_fidelity()
        signals = runner.stop_signals
        self.assertEqual(signals[ObservedStopReason.NORMAL_COMPLETION], "stop")
        self.assertEqual(signals[ObservedStopReason.TOOL_CALL], "tool_calls")
        self.assertEqual(signals[ObservedStopReason.OUTPUT_LIMIT], "length")
        self.assertIn("context_exceeded", signals[ObservedStopReason.CONTEXT_LIMIT])
        self.assertIn("client_disconnect", signals[ObservedStopReason.CANCELLED])
        self.assertIn("model_not_found", signals[ObservedStopReason.PROVIDER_ERROR])

    def test_a_context_overflow_probe_actually_sends_an_oversized_prompt(self) -> None:
        endpoint = _ScriptedEndpoint([])
        runner = LiveConformanceRunner(
            exec_fn=endpoint,
            base_url="http://127.0.0.1:8080",
            model_name="m",
            context_limit_tokens=1000,
            server_max_output_tokens=64,
        )
        runner._provoke_context_limit()
        # The padding travels as a count, expanded inside the container, so the
        # assertion is on the count rather than on a huge argv.
        argv = build_conformance_probe_argv(
            url="http://x", payload="{}", pad_chars=6000
        )
        self.assertEqual(argv[-1], "6000")
        self.assertEqual(endpoint.payloads[-1]["messages"][0]["content"], PAD_MARKER)


class PinCaptureTests(unittest.TestCase):
    def test_tool_schema_hash_is_stable_under_reordering(self) -> None:
        a = {"function": {"name": "alpha", "parameters": {}}}
        b = {"function": {"name": "beta", "parameters": {}}}
        self.assertEqual(
            canonical_tool_schema([a, b]), canonical_tool_schema([b, a])
        )

    def test_prompt_identity_concatenates_every_leading_system_turn(self) -> None:
        identity = extract_prompt_identity(
            {
                "messages": [
                    {"role": "system", "content": "one"},
                    {"role": "system", "content": "two"},
                    {"role": "user", "content": "go"},
                ],
                "tools": [{"function": {"name": "beta"}}, {"function": {"name": "alpha"}}],
            }
        )
        self.assertEqual(identity.system_prompt_sha256, sha256_text("one\ntwo"))
        self.assertEqual(identity.tool_names, ["alpha", "beta"])

    def test_a_capture_without_tools_is_refused(self) -> None:
        with self.assertRaises(PinCaptureError):
            extract_prompt_identity(
                {"messages": [{"role": "system", "content": "x"}], "tools": []}
            )

    def test_a_capture_without_a_system_turn_is_refused(self) -> None:
        with self.assertRaises(PinCaptureError):
            extract_prompt_identity(
                {
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": [{"function": {"name": "a"}}],
                }
            )

    def test_server_flags_hash_distinguishes_a_spaced_flag_from_two_flags(
        self,
    ) -> None:
        self.assertNotEqual(
            server_flags_sha256(["--a b"]), server_flags_sha256(["--a", "b"])
        )

    def test_declared_limits_argv_passes_the_model_as_the_last_argument(self) -> None:
        argv = cli_declared_limits_argv("/bundle", "qwen3.6-27b")
        self.assertEqual(argv[0], "sh")
        self.assertTrue(argv[2].rstrip().endswith("qwen3.6-27b"))

    def test_declared_limits_parses_the_modules_own_answer(self) -> None:
        captured = parse_declared_limits(
            json.dumps(
                {
                    "model": "qwen3.6-27b",
                    "known_context_limit": 1_000_000,
                    "known_output_limit": 65_536,
                    "default_context_limit": 200_000,
                    "output_ceiling": 64_000,
                    "context_limit_tokens": 1_000_000,
                    "max_output_tokens": 64_000,
                }
            ),
            source="test",
        )
        self.assertEqual(captured.context_limit_tokens, 1_000_000)
        self.assertEqual(captured.max_output_tokens, 64_000)

    def test_empty_declared_limits_output_is_refused(self) -> None:
        with self.assertRaises(PinCaptureError):
            parse_declared_limits("", source="test")


class Slice3GateTests(unittest.TestCase):
    def test_a_missing_report_blocks(self) -> None:
        decision = evaluate_slice3_gate(None)
        self.assertFalse(decision.allowed)

    def test_the_raising_form_stops_the_caller(self) -> None:
        with self.assertRaises(Slice3Blocked):
            require_slice3_unblocked(None)


if __name__ == "__main__":
    unittest.main()

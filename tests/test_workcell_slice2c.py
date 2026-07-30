"""Slice 2C: output-budget enforcement, the echo envelope, and the config pin.

Three separate boundaries are covered here and they are deliberately not mixed:

* `classify_request_body` is pure classification, tested directly, the way
  `relay_policy`'s other rules are.
* `DeterministicEchoProvider` is exercised over real HTTP, because the property
  it exists to establish is a byte-level transport property and an in-process
  call would not test it.
* `parse_effective_config` and `measure_transcription_fidelity` are tested on
  fixed inputs, since both are decision functions over text somebody else
  produced.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import unittest

from apoapsis.workcell.conformance import (
    ConformanceStatus,
    check_envelope_integrity,
)
from apoapsis.workcell.echo_provider import (
    ECHO_BEGIN,
    ECHO_END,
    DeterministicEchoProvider,
    extract_marked_payload,
)
from apoapsis.workcell.events import WorkcellEventAdapter
from apoapsis.workcell.pin_capture import (
    PinCaptureError,
    extract_resolved_limits,
    parse_effective_config,
)
from apoapsis.workcell.relay_policy import (
    ModelRelayConfig,
    RelayRejection,
    classify_request_body,
    observed_output_budget,
)
from apoapsis.workcell.transcription import (
    TranscriptionAttribution,
    measure_transcription_fidelity,
)

_PAYLOAD = "def résumé(x):\n    \"\"\"Naïve — ‘quoted’, emoji: 🛰️, CJK: 光年.\"\"\"\n"


def _config(**overrides: object) -> ModelRelayConfig:
    values: dict[str, object] = {
        "upstream_base_url": "http://127.0.0.1:8080",
        "socket_path": "/run/apoapsis/model.sock",
        "max_output_tokens": 16_384,
    }
    values.update(overrides)
    return ModelRelayConfig(**values)  # type: ignore[arg-type]


def _body(**payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


class ObservedOutputBudgetTests(unittest.TestCase):
    """The evidence half of the cap: what the relay saw, not what it blocked.

    Kept distinct from the enforcement tests because a peak that silently
    reports 0 for traffic it never inspected would make the Slice 2C claim
    "no outbound request exceeded 16,384" true and worthless at the same time.
    """

    def test_the_largest_named_budget_is_reported_with_its_key(self) -> None:
        observed = observed_output_budget(
            upstream_path="/v1/chat/completions",
            body=_body(max_tokens=4_096, max_completion_tokens=8_192),
        )
        assert observed is not None
        self.assertEqual(observed.tokens, 8_192)
        self.assertEqual(observed.key, "max_completion_tokens")

    def test_a_request_naming_no_budget_is_none_rather_than_zero(self) -> None:
        """`None` means the server's `-n` governs; `0` would be a claim."""

        self.assertIsNone(
            observed_output_budget(
                upstream_path="/v1/chat/completions",
                body=_body(model="m", messages=[]),
            )
        )

    def test_an_uninspectable_body_reports_nothing_rather_than_guessing(
        self,
    ) -> None:
        for body in (b"", b"not json", b"[1,2,3]", b"\xff\xfe\x00"):
            with self.subTest(body=body):
                self.assertIsNone(
                    observed_output_budget(
                        upstream_path="/v1/chat/completions", body=body
                    )
                )

    def test_a_route_without_an_output_budget_is_not_inspected(self) -> None:
        self.assertIsNone(
            observed_output_budget(
                upstream_path="/v1/models", body=_body(max_tokens=99)
            )
        )

    def test_the_observation_agrees_with_the_refusal_boundary(self) -> None:
        """Enforcement and evidence must not drift apart."""

        for tokens, allowed in ((16_384, True), (16_385, False)):
            with self.subTest(tokens=tokens):
                body = _body(max_tokens=tokens)
                observed = observed_output_budget(
                    upstream_path="/v1/chat/completions", body=body
                )
                decision = classify_request_body(
                    upstream_path="/v1/chat/completions",
                    body=body,
                    config=_config(),
                )
                assert observed is not None
                self.assertEqual(observed.tokens, tokens)
                self.assertIs(decision.allowed, allowed)


class OutputBudgetEnforcementTests(unittest.TestCase):
    def test_a_request_above_the_cap_is_refused_not_clamped(self) -> None:
        decision = classify_request_body(
            upstream_path="/v1/chat/completions",
            body=_body(model="m", max_tokens=64_000),
            config=_config(),
        )
        self.assertFalse(decision.allowed)
        self.assertIs(decision.rejection, RelayRejection.OUTPUT_BUDGET_ABOVE_CAP)
        self.assertEqual(decision.status, 400)
        # The refusal must be legible enough to act on: the number asked for,
        # the cap, and the key. A bare "rejected" would send an operator back
        # to the relay source.
        self.assertIn("64,000", decision.detail)
        self.assertIn("16,384", decision.detail)
        self.assertIn("max_tokens", decision.detail)

    def test_a_request_at_the_cap_is_allowed(self) -> None:
        decision = classify_request_body(
            upstream_path="/v1/chat/completions",
            body=_body(model="m", max_tokens=16_384),
            config=_config(),
        )
        self.assertTrue(decision.allowed)

    def test_every_provider_output_budget_key_is_enforced(self) -> None:
        for key in ("max_tokens", "max_completion_tokens", "max_new_tokens"):
            with self.subTest(key=key):
                decision = classify_request_body(
                    upstream_path="/v1/chat/completions",
                    body=_body(**{"model": "m", key: 20_000}),
                    config=_config(),
                )
                self.assertFalse(decision.allowed, key)

    def test_no_cap_configured_means_no_body_inspection(self) -> None:
        decision = classify_request_body(
            upstream_path="/v1/chat/completions",
            body=_body(max_tokens=10**9),
            config=_config(max_output_tokens=None),
        )
        self.assertTrue(decision.allowed)

    def test_non_generation_routes_are_not_inspected(self) -> None:
        decision = classify_request_body(
            upstream_path="/v1/models",
            body=_body(max_tokens=10**9),
            config=_config(),
        )
        self.assertTrue(decision.allowed)

    def test_a_non_json_body_is_forwarded_rather_than_adjudicated(self) -> None:
        """The relay is not a schema validator; see `classify_request_body`."""

        for body in (b"not json at all", b"[1, 2, 3]", b"\xff\xfe\x00"):
            with self.subTest(body=body):
                decision = classify_request_body(
                    upstream_path="/v1/chat/completions",
                    body=body,
                    config=_config(),
                )
                self.assertTrue(decision.allowed)

    def test_a_boolean_is_not_treated_as_a_budget(self) -> None:
        decision = classify_request_body(
            upstream_path="/v1/chat/completions",
            body=_body(max_tokens=True),
            config=_config(),
        )
        self.assertTrue(decision.allowed)

    def test_the_query_string_does_not_defeat_route_matching(self) -> None:
        decision = classify_request_body(
            upstream_path="/v1/chat/completions?stream=1",
            body=_body(max_tokens=64_000),
            config=_config(),
        )
        self.assertFalse(decision.allowed)


class EchoProviderTests(unittest.TestCase):
    def test_the_payload_survives_a_real_http_round_trip_byte_for_byte(self) -> None:
        with DeterministicEchoProvider() as provider:
            host, port = provider.base_url.rsplit(":", 1)
            connection = http.client.HTTPConnection(
                host.replace("http://", ""), int(port), timeout=10
            )
            request = json.dumps(
                {
                    "model": "apoapsis-echo",
                    "messages": [
                        {"role": "user", "content": ECHO_BEGIN + _PAYLOAD + ECHO_END}
                    ],
                }
            ).encode("utf-8")
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=request,
                headers={"Content-Type": "application/json"},
            )
            raw = connection.getresponse().read()
            connection.close()

            exchange = provider.last_exchange()
            self.assertIsNotNone(exchange)
            assert exchange is not None
            self.assertEqual(exchange.request_bytes, request)
            self.assertEqual(exchange.payload, _PAYLOAD)

            arguments = json.loads(
                json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"][
                    "arguments"
                ]
            )
            result = check_envelope_integrity(
                sent_bytes=(exchange.payload or "").encode("utf-8"),
                received_bytes=arguments["content"].encode("utf-8"),
            )
            self.assertIs(result.status, ConformanceStatus.PASSED)

    def test_the_evidence_record_does_not_normalise_whitespace(self) -> None:
        """Regression: `StrictModel` strips strings, and that broke this check.

        The first version of `EchoExchange` inherited
        `str_strip_whitespace=True` and quietly dropped the payload's trailing
        newline, so the envelope check compared a trimmed copy against the
        original and would have reported an adapter corruption that the
        *evidence model* had introduced. Exactly the failure mode ADR 0078 is
        about, one layer further in.
        """

        from apoapsis.workcell.echo_provider import EchoExchange

        payload = "\n  padded  \n"
        exchange = EchoExchange(path="/x", request_bytes=b"{}", payload=payload)
        self.assertEqual(exchange.payload, payload)

    def test_an_unmarked_request_is_reported_rather_than_guessed_at(self) -> None:
        self.assertIsNone(
            extract_marked_payload(
                json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
            )
        )

    def test_the_marker_is_found_regardless_of_message_position(self) -> None:
        body = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "you are a tool"},
                    {"role": "user", "content": "unrelated"},
                    {"role": "user", "content": ECHO_BEGIN + _PAYLOAD + ECHO_END},
                ]
            }
        ).encode("utf-8")
        self.assertEqual(extract_marked_payload(body), _PAYLOAD)


class EnvelopeIntegrityClassificationTests(unittest.TestCase):
    def test_truncation_is_named_as_truncation(self) -> None:
        sent = _PAYLOAD.encode("utf-8")
        result = check_envelope_integrity(sent_bytes=sent, received_bytes=sent[:-10])
        self.assertIs(result.status, ConformanceStatus.FAILED)
        self.assertIn("truncated", result.detail)

    def test_double_encoding_is_named_as_double_encoding(self) -> None:
        sent = _PAYLOAD.encode("utf-8")
        mangled = _PAYLOAD.encode("utf-8").decode("latin-1").encode("utf-8")
        result = check_envelope_integrity(sent_bytes=sent, received_bytes=mangled)
        self.assertIs(result.status, ConformanceStatus.FAILED)
        self.assertIn("double-encoded", result.detail)

    def test_an_empty_response_is_a_failure_not_a_pass(self) -> None:
        result = check_envelope_integrity(
            sent_bytes=_PAYLOAD.encode("utf-8"), received_bytes=b""
        )
        self.assertIs(result.status, ConformanceStatus.FAILED)


class EffectiveConfigPinTests(unittest.TestCase):
    def _payload(self, **overrides: object) -> str:
        base: dict[str, object] = {
            "merged_settings": {"selectedAuthType": "openai"},
            "resolved_generation_config": {
                "model": "qwen3.6-27b",
                "baseUrl": "http://127.0.0.1:8080/v1",
                "generationConfig": {
                    "contextWindowSize": 65_536,
                    "samplingParams": {"max_tokens": 16_384},
                },
            },
            "sources": {"contextWindowSize": {"kind": "settings"}},
            "warnings": [],
            "redacted_keys": ["security.auth.apiKey"],
        }
        base.update(overrides)
        return json.dumps(base)

    def test_the_digest_covers_the_resolved_configuration(self) -> None:
        config = parse_effective_config(self._payload(), source="unit")
        expected = hashlib.sha256(
            json.dumps(
                {
                    "merged_settings": {"selectedAuthType": "openai"},
                    "resolved_generation_config": json.loads(self._payload())[
                        "resolved_generation_config"
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(config.effective_config_sha256, expected)

    def test_a_changed_window_moves_the_digest(self) -> None:
        first = parse_effective_config(self._payload(), source="unit")
        second = parse_effective_config(
            self._payload(
                resolved_generation_config={
                    "model": "qwen3.6-27b",
                    "baseUrl": "http://127.0.0.1:8080/v1",
                    "generationConfig": {
                        "contextWindowSize": 32_768,
                        "samplingParams": {"max_tokens": 16_384},
                    },
                }
            ),
            source="unit",
        )
        self.assertNotEqual(
            first.effective_config_sha256, second.effective_config_sha256
        )

    def test_a_reworded_warning_does_not_move_the_digest(self) -> None:
        first = parse_effective_config(self._payload(), source="unit")
        second = parse_effective_config(
            self._payload(warnings=["a new CLI release says something else"]),
            source="unit",
        )
        self.assertEqual(first.effective_config_sha256, second.effective_config_sha256)

    def test_resolved_limits_are_read_with_their_provenance(self) -> None:
        limits = extract_resolved_limits(
            parse_effective_config(self._payload(), source="unit")
        )
        self.assertEqual(limits.context_window_size, 65_536)
        self.assertEqual(limits.max_output_tokens, 16_384)
        self.assertIn("settings", limits.context_window_source)

    def test_a_missing_override_raises_rather_than_defaulting(self) -> None:
        with self.assertRaises(PinCaptureError):
            extract_resolved_limits(
                parse_effective_config(
                    self._payload(
                        resolved_generation_config={
                            "model": "qwen3.6-27b",
                            "generationConfig": {},
                        }
                    ),
                    source="unit",
                )
            )

    def test_empty_output_is_refused_rather_than_pinned_as_empty(self) -> None:
        with self.assertRaises(PinCaptureError):
            parse_effective_config("", source="unit")


class TranscriptionFidelityTests(unittest.TestCase):
    def test_an_exact_transcription_is_recorded_as_exact(self) -> None:
        record = measure_transcription_fidelity(sent=_PAYLOAD, received=_PAYLOAD)
        self.assertTrue(record.exact)
        self.assertFalse(record.gating)

    def test_a_quote_substitution_is_named_by_codepoint(self) -> None:
        record = measure_transcription_fidelity(
            sent="say ‘hi’", received="say 'hi'"
        )
        self.assertFalse(record.exact)
        self.assertEqual(len(record.differences), 2)
        self.assertIn("QUOTATION MARK", record.differences[0].sent_name)

    def test_the_record_always_attributes_itself_to_the_model(self) -> None:
        for received in (None, "x", _PAYLOAD):
            with self.subTest(received=received):
                record = measure_transcription_fidelity(
                    sent=_PAYLOAD, received=received
                )
                self.assertIs(
                    record.attribution, TranscriptionAttribution.MODEL_BEHAVIOUR
                )
                self.assertFalse(record.gating)

    def test_an_absent_response_is_unmeasured_rather_than_a_failure(self) -> None:
        record = measure_transcription_fidelity(sent=_PAYLOAD, received=None)
        self.assertFalse(record.measured)
        self.assertFalse(record.exact)


class NestedEventEnvelopeTests(unittest.TestCase):
    """The CLI nests tool calls inside `message.content`; see `feed_event`.

    These exist because the flat-schema adapter failed *silently* on the real
    stream: zero malformed lines, zero unrecognised types, and zero tool calls
    across a session that made forty-four of them. The paired spike read that
    as seven lost capabilities. A regression here would look like a capability
    finding again, so it is pinned down rather than left to the live run.
    """

    def _adapter(self) -> WorkcellEventAdapter:
        return WorkcellEventAdapter(
            context_limit_tokens=65_536, max_output_tokens=16_384
        )

    def test_a_nested_tool_use_block_is_counted_as_a_tool_call(self) -> None:
        adapter = self._adapter()
        adapter.feed_event(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "read_file",
                            "input": {"file_path": "/task/task.md"},
                        }
                    ],
                },
            }
        )
        trace = adapter.finish()
        self.assertEqual(len(trace.tool_calls), 1)
        self.assertEqual(trace.tool_calls[0].tool_name, "read_file")
        # `input` is the CLI's name for the argument object.
        self.assertEqual(trace.tool_calls[0].argument_keys, ["file_path"])

    def test_a_nested_tool_result_closes_its_call(self) -> None:
        adapter = self._adapter()
        adapter.feed_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "c1", "name": "glob", "input": {}}
                    ]
                },
            }
        )
        adapter.feed_event(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "is_error": False,
                            "content": "ok",
                        }
                    ]
                },
            }
        )
        trace = adapter.finish()
        self.assertEqual(trace.malformed_lines, 0)
        # A call that got its result is not reported as never having returned.
        self.assertEqual(trace.errors, [])
        self.assertFalse(trace.tool_calls[0].failed)

    def test_is_error_marks_failure_without_inventing_an_exit_code(self) -> None:
        adapter = self._adapter()
        adapter.feed_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "c1", "name": "edit", "input": {}}
                    ]
                },
            }
        )
        adapter.feed_event(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "is_error": True,
                            "content": "boom",
                        }
                    ]
                },
            }
        )
        record = adapter.finish().tool_calls[0]
        self.assertTrue(record.failed)
        self.assertIsNone(record.exit_code)

    def test_an_envelope_without_a_message_is_malformed_not_ignored(self) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "assistant"})
        self.assertEqual(adapter.finish().malformed_lines, 1)

    def test_session_totals_are_adopted_only_when_nothing_else_reported(
        self,
    ) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "result", "usage": {"input_tokens": 900}})
        self.assertEqual(adapter.finish().input_tokens, 900)

        other = self._adapter()
        other.feed_event(
            {
                "type": "assistant",
                "message": {"content": [], "usage": {"input_tokens": 10}},
            }
        )
        other.feed_event({"type": "result", "usage": {"input_tokens": 900}})
        # Not 910: the summary must not be added on top of per-message usage.
        self.assertEqual(other.finish().input_tokens, 10)

    def test_the_cli_spelling_of_cached_tokens_is_understood(self) -> None:
        adapter = self._adapter()
        adapter.feed_event(
            {
                "type": "assistant",
                "message": {
                    "content": [],
                    "usage": {"input_tokens": 5, "cache_read_input_tokens": 7},
                },
            }
        )
        self.assertEqual(adapter.finish().cached_input_tokens, 7)


if __name__ == "__main__":
    unittest.main()

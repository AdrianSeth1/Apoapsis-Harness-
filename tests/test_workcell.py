from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoapsis.evaluation.paired import BaselineCapability, CapabilityStatus
from apoapsis.models.ceilings import CeilingStopReason
from apoapsis.workcell.conformance import (
    CheckResult,
    ConformanceCheck,
    ConformanceStatus,
    ObservedStopReason,
    check_declared_limits_match_server,
    check_multiline_unicode_integrity,
    check_replay_non_idempotence_guard,
    check_stop_reason_fidelity,
    check_thinking_block_handling,
    evaluate_conformance,
)
from apoapsis.workcell.containment import (
    DEFAULT_CONTAINMENT_PROBES,
    ContainmentCategory,
    ContainmentProbe,
    ProbeExpectation,
    ProbeResult,
    ProbeStatus,
    classify_probe,
    evaluate_containment,
)
from apoapsis.workcell.controller import (
    CleanupRecord,
    WorkcellController,
    WorkcellRunRecord,
)
from apoapsis.workcell.events import WorkcellEventAdapter
from apoapsis.workcell.pins import (
    AgentCliPin,
    ContainerPin,
    EgressPolicy,
    ModelPin,
    RelayPin,
    WorkcellConfig,
    WorkcellPin,
)
from apoapsis.workcell.relay_policy import ALLOWED_ROUTES, ModelRelayConfig
from apoapsis.workcell.spike import (
    SpikeVerdict,
    build_spike_report,
    observe_capabilities,
)

_SHA = "a" * 64
_DIGEST = "sha256:" + "b" * 64


def _pin(**overrides) -> WorkcellPin:
    payload = {
        "model": ModelPin(
            model_name="qwen3.6-27b",
            model_file_sha256=_SHA,
            quantization="Q4_K_M",
            server_name="llama-server",
            server_version="b4321",
            server_flags_sha256=_SHA,
            context_limit_tokens=65_536,
            max_output_tokens=16_384,
            sampling_seed=7,
            temperature=0.0,
            chat_template_sha256=_SHA,
            endpoint="http://127.0.0.1:8080/v1",
        ),
        "agent_cli": AgentCliPin(
            cli_name="qwen-code",
            cli_version="1.2.3",
            cli_sha256=_SHA,
            is_default_distribution=True,
            system_prompt_sha256=_SHA,
            tool_schema_sha256=_SHA,
            effective_config_sha256=_SHA,
            tool_names=["glob", "read_file", "run_shell_command", "write_file"],
        ),
        "container": ContainerPin(
            image="apoapsis/workcell",
            image_digest=_DIGEST,
            runtime_version="27.0.3",
        ),
        "relay": RelayPin(
            relay_version="1.0",
            forwarder_version="1.0",
            forwarder_sha256=_SHA,
            allowed_routes=sorted(
                f"{method} {path}" for method, path in ALLOWED_ROUTES
            ),
            upstream_base_url="http://127.0.0.1:8080",
        ),
        "seed_commit": "1" * 40,
        "task_artifact_sha256": _SHA,
        "verifier_version": "v1",
    }
    payload.update(overrides)
    return WorkcellPin(**payload)


def _egress(tmp: Path) -> EgressPolicy:
    forwarder = tmp / "tooling" / "forwarder.py"
    forwarder.parent.mkdir(parents=True, exist_ok=True)
    forwarder.write_text("# forwarder", encoding="utf-8")
    return EgressPolicy(
        relay=ModelRelayConfig(
            upstream_base_url="http://127.0.0.1:8080",
            socket_path=str(tmp / "run" / "model.sock"),
        ),
        forwarder_host_path=str(forwarder),
    )


def _config(tmp: Path, **overrides) -> WorkcellConfig:
    workspace = tmp / "clone"
    workspace.mkdir(parents=True, exist_ok=True)
    artifact = tmp / "task" / "task.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("objective", encoding="utf-8")
    payload = {
        "pin": _pin(),
        "workspace_host_path": str(workspace),
        "task_artifact_host_path": str(artifact),
        "egress": _egress(tmp),
    }
    payload.update(overrides)
    return WorkcellConfig(**payload)


class PinTests(unittest.TestCase):
    def test_digest_is_stable_and_identity_sensitive(self) -> None:
        self.assertEqual(_pin().manifest_digest(), _pin().manifest_digest())
        other = _pin(seed_commit="2" * 40)
        self.assertNotEqual(_pin().manifest_digest(), other.manifest_digest())

    def test_output_cap_above_the_window_is_refused(self) -> None:
        # A run pinned this way would report output truncation for what is
        # actually context exhaustion.
        with self.assertRaises(ValueError):
            ModelPin(
                model_name="q",
                model_file_sha256=_SHA,
                quantization="Q4_K_M",
                server_name="llama-server",
                server_version="b1",
                server_flags_sha256=_SHA,
                context_limit_tokens=8_192,
                max_output_tokens=16_384,
                sampling_seed=1,
                temperature=0.0,
                chat_template_sha256=_SHA,
                endpoint="http://127.0.0.1:8080/v1",
            )

    def test_tool_names_must_be_sorted_and_unique(self) -> None:
        for names in (["write_file", "glob"], ["glob", "glob"]):
            with self.assertRaises(ValueError):
                AgentCliPin(
                    cli_name="qwen-code",
                    cli_version="1",
                    cli_sha256=_SHA,
                    is_default_distribution=True,
                    system_prompt_sha256=_SHA,
                    tool_schema_sha256=_SHA,
                    effective_config_sha256=_SHA,
                    tool_names=names,
                )

    def test_task_artifact_inside_the_workspace_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "clone"
            workspace.mkdir()
            inside = workspace / "task.md"
            inside.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                WorkcellConfig(
                    pin=_pin(),
                    workspace_host_path=str(workspace),
                    task_artifact_host_path=str(inside),
                    egress=_egress(root),
                )

    def test_every_identity_field_is_required(self) -> None:
        payload = _pin().model_dump(mode="json")
        del payload["model"]["chat_template_sha256"]
        with self.assertRaises(ValueError):
            WorkcellPin.model_validate(payload)


class ContainmentTests(unittest.TestCase):
    def _probe(self, expectation: ProbeExpectation) -> ContainmentProbe:
        return ContainmentProbe(
            probe_id="p",
            category=ContainmentCategory.NETWORK,
            argv=["true"],
            expectation=expectation,
            expected_stdout="65532"
            if expectation == ProbeExpectation.STDOUT_EQUALS
            else None,
            breach_meaning="the boundary is open",
        )

    def test_refused_command_is_contained_and_success_is_a_breach(self) -> None:
        probe = self._probe(ProbeExpectation.COMMAND_REFUSED)
        self.assertEqual(
            classify_probe(probe, exit_code=1).status, ProbeStatus.CONTAINED
        )
        self.assertEqual(
            classify_probe(probe, exit_code=0).status, ProbeStatus.BREACHED
        )

    def test_an_incomplete_probe_is_inconclusive_not_safe(self) -> None:
        # "It errored, so we must be safe" is how a hole stays open.
        probe = self._probe(ProbeExpectation.COMMAND_REFUSED)
        self.assertEqual(
            classify_probe(probe, exit_code=None).status, ProbeStatus.INCONCLUSIVE
        )

    def test_stdout_mismatch_is_a_breach_and_failure_is_inconclusive(self) -> None:
        probe = self._probe(ProbeExpectation.STDOUT_EQUALS)
        self.assertEqual(
            classify_probe(probe, exit_code=0, stdout="65532\n").status,
            ProbeStatus.CONTAINED,
        )
        self.assertEqual(
            classify_probe(probe, exit_code=0, stdout="0").status,
            ProbeStatus.BREACHED,
        )
        self.assertEqual(
            classify_probe(probe, exit_code=1, stdout="").status,
            ProbeStatus.INCONCLUSIVE,
        )

    def test_command_succeeds_expectation(self) -> None:
        probe = self._probe(ProbeExpectation.COMMAND_SUCCEEDS)
        self.assertEqual(
            classify_probe(probe, exit_code=0).status, ProbeStatus.CONTAINED
        )
        self.assertEqual(
            classify_probe(probe, exit_code=1).status, ProbeStatus.BREACHED
        )

    def _all_contained(self) -> list[ProbeResult]:
        return [
            ProbeResult(probe_id=probe.probe_id, status=ProbeStatus.CONTAINED)
            for probe in DEFAULT_CONTAINMENT_PROBES
        ]

    def test_a_complete_clean_suite_is_contained(self) -> None:
        report = evaluate_containment(
            self._all_contained(), workcell_manifest_digest=_SHA
        )
        self.assertTrue(report.contained)
        self.assertEqual(report.breaches, [])

    def test_a_missing_probe_result_fails_closed(self) -> None:
        # The suite that quietly shrank is the failure mode most likely to
        # survive review.
        results = self._all_contained()[:-1]
        report = evaluate_containment(results, workcell_manifest_digest=_SHA)
        self.assertFalse(report.contained)
        self.assertIn(
            DEFAULT_CONTAINMENT_PROBES[-1].probe_id, report.unproven
        )
        self.assertIn("not a closed one", report.detail)

    def test_one_breach_fails_the_whole_report(self) -> None:
        results = self._all_contained()
        results[0] = ProbeResult(
            probe_id=results[0].probe_id, status=ProbeStatus.BREACHED
        )
        report = evaluate_containment(results, workcell_manifest_digest=_SHA)
        self.assertFalse(report.contained)
        self.assertEqual(len(report.breaches), 1)

    def test_the_suite_covers_every_required_category(self) -> None:
        covered = {probe.category for probe in DEFAULT_CONTAINMENT_PROBES}
        self.assertEqual(covered, set(ContainmentCategory))

    def test_the_suite_covers_the_named_threats(self) -> None:
        ids = {probe.probe_id for probe in DEFAULT_CONTAINMENT_PROBES}
        for required in (
            "docker-socket-absent",
            "podman-socket-absent",
            "wsl-interop-absent",
            "cloud-metadata-unreachable",
            "no-external-route",
            "ssh-agent-absent",
            "home-credentials-absent",
            "host-windows-users-absent",
            "audit-log-unreachable",
            "non-root-execution",
        ):
            self.assertIn(required, ids)

    def test_an_unexpected_extra_result_is_reported(self) -> None:
        results = self._all_contained()
        results.append(
            ProbeResult(probe_id="invented", status=ProbeStatus.CONTAINED)
        )
        report = evaluate_containment(results, workcell_manifest_digest=_SHA)
        self.assertIn("invented", report.detail)


class ConformanceTests(unittest.TestCase):
    def test_length_covering_both_limits_is_tolerated(self) -> None:
        # Token counts disambiguate it and the ceiling classifier uses exactly
        # that, so this is not a conformance failure.
        result = check_stop_reason_fidelity(
            {
                ObservedStopReason.NORMAL_COMPLETION: "stop",
                ObservedStopReason.TOOL_CALL: "tool_calls",
                ObservedStopReason.CONTEXT_LIMIT: "length",
                ObservedStopReason.OUTPUT_LIMIT: "length",
                ObservedStopReason.CANCELLED: "cancelled",
                ObservedStopReason.PROVIDER_ERROR: "error",
            }
        )
        self.assertEqual(result.status, ConformanceStatus.PASSED)

    def test_collapsing_completion_into_a_limit_fails(self) -> None:
        result = check_stop_reason_fidelity(
            {
                ObservedStopReason.NORMAL_COMPLETION: "stop",
                ObservedStopReason.TOOL_CALL: "stop",
                ObservedStopReason.CONTEXT_LIMIT: "length",
                ObservedStopReason.OUTPUT_LIMIT: "length",
                ObservedStopReason.CANCELLED: "cancelled",
                ObservedStopReason.PROVIDER_ERROR: "error",
            }
        )
        self.assertEqual(result.status, ConformanceStatus.FAILED)

    def test_an_uncaptured_stop_reason_is_not_run(self) -> None:
        result = check_stop_reason_fidelity(
            {ObservedStopReason.NORMAL_COMPLETION: "stop"}
        )
        self.assertEqual(result.status, ConformanceStatus.NOT_RUN)

    def test_unicode_round_trip_diagnoses_each_corruption(self) -> None:
        text = "def f():\n    return 'café ✓'\n"
        self.assertEqual(
            check_multiline_unicode_integrity(sent=text, received=text).status,
            ConformanceStatus.PASSED,
        )
        truncated = check_multiline_unicode_integrity(
            sent=text, received=text[:10]
        )
        self.assertEqual(truncated.status, ConformanceStatus.FAILED)
        self.assertIn("truncated", truncated.detail)
        escaped = check_multiline_unicode_integrity(
            sent=text, received=text.replace("\n", "\\n")
        )
        self.assertIn("escaped", escaped.detail)
        doubled = check_multiline_unicode_integrity(
            sent=text, received=text.encode("utf-8").decode("latin-1")
        )
        self.assertIn("double-encoded", doubled.detail)

    def test_non_idempotent_stripping_fails(self) -> None:
        self.assertEqual(
            check_thinking_block_handling(
                supported=False, stripped_once="a", stripped_twice="a"
            ).status,
            ConformanceStatus.PASSED,
        )
        self.assertEqual(
            check_thinking_block_handling(
                supported=False, stripped_once="a<think>b", stripped_twice="a"
            ).status,
            ConformanceStatus.FAILED,
        )

    def test_declared_limits_must_match_the_server(self) -> None:
        self.assertEqual(
            check_declared_limits_match_server(
                cli_context_limit=65_536,
                cli_max_output=16_384,
                server_context_limit=65_536,
                server_max_output=16_384,
            ).status,
            ConformanceStatus.PASSED,
        )
        mismatch = check_declared_limits_match_server(
            cli_context_limit=131_072,
            cli_max_output=16_384,
            server_context_limit=65_536,
            server_max_output=16_384,
        )
        self.assertEqual(mismatch.status, ConformanceStatus.FAILED)
        self.assertIn("context limit", mismatch.detail)

    def test_replay_must_not_duplicate_a_mutating_tool(self) -> None:
        self.assertEqual(
            check_replay_non_idempotence_guard(
                mutating_tool_call_id="c1", executed_call_ids=["c1"]
            ).status,
            ConformanceStatus.PASSED,
        )
        self.assertEqual(
            check_replay_non_idempotence_guard(
                mutating_tool_call_id="c1", executed_call_ids=["c1", "c1"]
            ).status,
            ConformanceStatus.FAILED,
        )
        self.assertEqual(
            check_replay_non_idempotence_guard(
                mutating_tool_call_id="c1", executed_call_ids=[]
            ).status,
            ConformanceStatus.INCONCLUSIVE,
        )

    def test_evaluation_fails_closed_on_a_missing_check(self) -> None:
        passing = [
            CheckResult(check=check, status=ConformanceStatus.PASSED)
            for check in ConformanceCheck
        ]
        self.assertTrue(
            evaluate_conformance(passing, workcell_manifest_digest=_SHA).conformant
        )
        report = evaluate_conformance(
            passing[:-1], workcell_manifest_digest=_SHA
        )
        self.assertFalse(report.conformant)
        self.assertIn(passing[-1].check, report.unproven)

    def test_a_failure_is_named_an_adapter_defect(self) -> None:
        results = [
            CheckResult(check=check, status=ConformanceStatus.PASSED)
            for check in ConformanceCheck
        ]
        results[0] = CheckResult(
            check=results[0].check, status=ConformanceStatus.FAILED
        )
        report = evaluate_conformance(results, workcell_manifest_digest=_SHA)
        self.assertIn("adapter defects, not model behaviour", report.detail)


class EventAdapterTests(unittest.TestCase):
    def _adapter(self) -> WorkcellEventAdapter:
        return WorkcellEventAdapter(
            context_limit_tokens=65_536,
            max_output_tokens=16_384,
            max_tool_output_chars=100,
        )

    def test_a_normal_session_is_folded_into_a_trace(self) -> None:
        adapter = self._adapter()
        for payload in (
            {"type": "session.start", "session_id": "s1"},
            {"type": "tool.call", "call_id": "c1", "name": "glob",
             "arguments": {"pattern": "**/*.py"}},
            {"type": "tool.result", "call_id": "c1", "output": "a.py",
             "exit_code": 0, "duration_seconds": 0.2},
            {"type": "usage", "input_tokens": 1000, "output_tokens": 200,
             "finish_reason": "tool_calls"},
            {"type": "session.end", "reason": "stop"},
        ):
            adapter.feed_line(json.dumps(payload))
        trace = adapter.finish()
        self.assertEqual(trace.session_id, "s1")
        self.assertEqual(trace.distinct_tools_used, ["glob"])
        self.assertEqual(trace.input_tokens, 1000)
        self.assertEqual(trace.model_requests, 1)
        self.assertTrue(trace.ended)
        self.assertEqual(trace.ceiling_events, [])

    def test_ready_for_evaluation_is_recorded_without_ending_anything(self) -> None:
        adapter = self._adapter()
        adapter.feed_event(
            {"type": "tool.call", "call_id": "c1", "name": "ready_for_evaluation"}
        )
        trace = adapter.finish()
        self.assertTrue(trace.ready_for_evaluation)
        # A request for inspection is not a completion decision.
        self.assertFalse(trace.ended)

    def test_context_exhaustion_then_provider_error_is_attributed(self) -> None:
        adapter = self._adapter()
        adapter.feed_event(
            {
                "type": "usage",
                "input_tokens": 64_409,
                "output_tokens": 1_127,
                "finish_reason": "length",
            }
        )
        adapter.feed_event({"type": "error", "message": "HTTP 500"})
        trace = adapter.finish()
        self.assertEqual(
            [item.reason for item in trace.ceiling_events],
            [
                CeilingStopReason.INPUT_CONTEXT_EXHAUSTED,
                CeilingStopReason.PROVIDER_ERROR_AFTER_ROLLOVER,
            ],
        )

    def test_a_provider_error_without_a_rollover_is_just_an_error(self) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "error", "message": "connection reset"})
        trace = adapter.finish()
        self.assertEqual(trace.ceiling_events, [])
        self.assertEqual(len(trace.errors), 1)

    def test_oversized_tool_output_records_a_truncation(self) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "tool.call", "call_id": "c1", "name": "bash"})
        adapter.feed_event(
            {"type": "tool.result", "call_id": "c1", "output": "x" * 500}
        )
        trace = adapter.finish()
        self.assertEqual(
            trace.ceiling_events[0].reason, CeilingStopReason.TOOL_OUTPUT_TRUNCATION
        )
        self.assertIn("not reversible", trace.ceiling_events[0].detail)

    def test_a_spill_artifact_makes_truncation_reversible(self) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "tool.call", "call_id": "c1", "name": "bash"})
        adapter.feed_event(
            {
                "type": "tool.result",
                "call_id": "c1",
                "output": "x" * 500,
                "output_artifact": "/task/artifacts/c1.log",
            }
        )
        trace = adapter.finish()
        self.assertIn("full output at", trace.ceiling_events[0].detail)

    def test_malformed_and_unrecognised_input_is_counted_not_dropped(self) -> None:
        adapter = self._adapter()
        adapter.feed_line('{"type": "usage", "input_tokens": 1')
        adapter.feed_line("[1, 2, 3]")
        adapter.feed_line(json.dumps({"type": "telemetry.new", "x": 1}))
        adapter.feed_line("   ")
        trace = adapter.finish()
        self.assertEqual(trace.malformed_lines, 2)
        self.assertEqual(trace.unrecognised_event_types, ["telemetry.new"])

    def test_an_unanswered_tool_call_is_an_error_at_finish(self) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "tool.call", "call_id": "c1", "name": "bash"})
        trace = adapter.finish()
        self.assertTrue(trace.tool_calls[0].failed)
        self.assertIn("never reported a result", trace.errors[0])

    def test_a_result_with_no_matching_call_is_malformed(self) -> None:
        adapter = self._adapter()
        adapter.feed_event({"type": "tool.result", "call_id": "ghost"})
        self.assertEqual(adapter.finish().malformed_lines, 1)


def _trace_from(events: list[dict]) -> object:
    adapter = WorkcellEventAdapter(
        context_limit_tokens=65_536, max_output_tokens=16_384
    )
    for event in events:
        adapter.feed_event(event)
    return adapter.finish()


def _capable_events() -> list[dict]:
    events: list[dict] = [{"type": "session.start", "session_id": "s1"}]
    plan = [
        ("c1", "glob"),
        ("c2", "read_file"),
        ("c3", "write_file"),
        ("c4", "write_file"),
        ("c5", "run_shell_command"),
        ("c6", "run_shell_command"),
    ]
    for call_id, name in plan:
        events.append({"type": "tool.call", "call_id": call_id, "name": name})
        events.append(
            {"type": "tool.result", "call_id": call_id, "output": "ok", "exit_code": 0}
        )
    events.append({"type": "compaction"})
    events.append({"type": "session.end", "reason": "stop"})
    return events


def _run_record() -> WorkcellRunRecord:
    return WorkcellRunRecord(
        run_id="r1",
        container_name="apoapsis-workcell-r1",
        workcell_manifest_digest=_SHA,
        cleanup=CleanupRecord(
            container_removed=True,
            ownership_verified=True,
            process_tree_terminated=True,
            model_socket_removed=True,
        ),
    )


class SpikeTests(unittest.TestCase):
    def _prerequisites(self):
        """An agent profile and readiness that both hold.

        Supplied explicitly because `build_spike_report` now treats their
        absence as disqualifying: a capability verdict about an unidentified
        agent whose tools were never exercised is what Slice 2C produced.
        """

        from apoapsis.workcell.agent_profile import ProfileGateResult
        from apoapsis.workcell.capability_readiness import (
            CapabilityReadinessReport,
            ReadinessOperation,
            ReadinessOperationResult,
            ReadinessStatus,
        )

        return (
            ProfileGateResult(ok=True, detail="coding profile confirmed"),
            CapabilityReadinessReport(
                results=[
                    ReadinessOperationResult(
                        operation=operation, status=ReadinessStatus.PASSED
                    )
                    for operation in ReadinessOperation
                ],
                ready=True,
                residue_free=True,
                detail="read, edit, and shell exercised",
            ),
        )

    def _clean_reports(self):
        containment = evaluate_containment(
            [
                ProbeResult(probe_id=probe.probe_id, status=ProbeStatus.CONTAINED)
                for probe in DEFAULT_CONTAINMENT_PROBES
            ],
            workcell_manifest_digest=_SHA,
        )
        conformance = evaluate_conformance(
            [
                CheckResult(check=check, status=ConformanceStatus.PASSED)
                for check in ConformanceCheck
            ],
            workcell_manifest_digest=_SHA,
        )
        return containment, conformance

    def test_a_capable_session_preserves_every_control_capability(self) -> None:
        containment, conformance = self._clean_reports()
        profile, readiness = self._prerequisites()
        report = build_spike_report(
            pin=_pin(),
            run=_run_record(),
            trace=_trace_from(_capable_events()),
            containment=containment,
            conformance=conformance,
            agent_profile=profile,
            capability_readiness=readiness,
        )
        self.assertEqual(report.verdict, SpikeVerdict.CAPABILITY_PRESERVED)
        self.assertEqual(report.lost_capabilities, [])
        # The control had no compaction; the workcell does.
        self.assertIn(
            BaselineCapability.CONTEXT_CONTINUATION_OR_COMPACTION,
            report.gained_capabilities,
        )
        self.assertFalse(report.acceptance_repair_performed)

    def test_no_command_after_an_edit_loses_the_self_directed_loop(self) -> None:
        # This is the Slice 2 failure mode exactly: the agent edits once and
        # never gets to look at its own work.
        events = [
            {"type": "session.start", "session_id": "s1"},
            {"type": "tool.call", "call_id": "c1", "name": "run_shell_command"},
            {"type": "tool.result", "call_id": "c1", "output": "", "exit_code": 0},
            {"type": "tool.call", "call_id": "c2", "name": "run_shell_command"},
            {"type": "tool.result", "call_id": "c2", "output": "", "exit_code": 0},
            {"type": "tool.call", "call_id": "c3", "name": "write_file"},
            {"type": "tool.result", "call_id": "c3", "output": "", "exit_code": 0},
            {"type": "session.end", "reason": "stop"},
        ]
        containment, conformance = self._clean_reports()
        profile, readiness = self._prerequisites()
        report = build_spike_report(
            pin=_pin(),
            run=_run_record(),
            trace=_trace_from(events),
            containment=containment,
            conformance=conformance,
            agent_profile=profile,
            capability_readiness=readiness,
        )
        self.assertEqual(report.verdict, SpikeVerdict.CAPABILITY_REGRESSED)
        self.assertIn(
            BaselineCapability.SELF_DIRECTED_TEST_DEBUG_LOOP,
            report.lost_capabilities,
        )

    def test_one_agent_issued_shell_call_proves_arbitrary_commands(self) -> None:
        # Slice 2D correction. This check originally required more than one
        # shell call, which made the measurement depend on task size: a run
        # that correctly needed exactly one command was recorded as having lost
        # the ability to run any. The trace only ever contains the agent's own
        # tool calls -- the harness's configured verification commands go
        # through `controller.exec` and never appear -- so the real boundary is
        # zero versus one, which is also where the legacy typed protocol sits.
        events = [
            {"type": "tool.call", "call_id": "c1", "name": "write_file"},
            {"type": "tool.result", "call_id": "c1", "output": "", "exit_code": 0},
            {"type": "tool.call", "call_id": "c2", "name": "run_shell_command"},
            {"type": "tool.result", "call_id": "c2", "output": "OK", "exit_code": 0},
        ]
        observed = {
            item.capability: item
            for item in observe_capabilities(_trace_from(events), run=_run_record())
        }
        entry = observed[BaselineCapability.ARBITRARY_SANDBOX_COMMANDS]
        self.assertEqual(entry.status, CapabilityStatus.PROVIDED)
        # ...while still saying plainly what one call does not establish.
        self.assertIn("does not by itself demonstrate variety", entry.evidence)

    def test_no_shell_call_still_means_no_arbitrary_commands(self) -> None:
        events = [
            {"type": "tool.call", "call_id": "c1", "name": "write_file"},
            {"type": "tool.result", "call_id": "c1", "output": "", "exit_code": 0},
        ]
        observed = {
            item.capability: item.status
            for item in observe_capabilities(_trace_from(events), run=_run_record())
        }
        self.assertEqual(
            observed[BaselineCapability.ARBITRARY_SANDBOX_COMMANDS],
            CapabilityStatus.UNPROVEN,
        )

    def test_a_failed_shell_call_does_not_prove_the_capability(self) -> None:
        events = [
            {"type": "tool.call", "call_id": "c1", "name": "run_shell_command"},
            {"type": "tool.result", "call_id": "c1", "exit_code": 127},
        ]
        observed = {
            item.capability: item.status
            for item in observe_capabilities(_trace_from(events), run=_run_record())
        }
        self.assertEqual(
            observed[BaselineCapability.ARBITRARY_SANDBOX_COMMANDS],
            CapabilityStatus.UNPROVEN,
        )

    def test_missing_prerequisites_are_not_measurable_not_a_regression(self) -> None:
        # The Slice 2C correction. That run measured genuine Qwen Code launched
        # as a read-only planner: no write_file, no edit, no run_shell_command.
        # It reported CAPABILITY_REGRESSED -- a claim that the harness took a
        # capability away, derived from a run in which the capability was never
        # present to take. Missing prerequisites invalidate an experiment; they
        # do not demonstrate a regression.
        containment, conformance = self._clean_reports()
        for profile, readiness in (
            (None, None),
            (self._prerequisites()[0], None),
            (None, self._prerequisites()[1]),
        ):
            report = build_spike_report(
                pin=_pin(),
                run=_run_record(),
                trace=_trace_from([]),
                containment=containment,
                conformance=conformance,
                agent_profile=profile,
                capability_readiness=readiness,
            )
            self.assertEqual(report.verdict, SpikeVerdict.NOT_MEASURABLE)
            self.assertNotEqual(report.verdict, SpikeVerdict.CAPABILITY_REGRESSED)

    def test_a_failed_agent_profile_blocks_the_capability_question(self) -> None:
        from apoapsis.workcell.agent_profile import ProfileGateResult

        containment, conformance = self._clean_reports()
        _, readiness = self._prerequisites()
        report = build_spike_report(
            pin=_pin(),
            run=_run_record(),
            trace=_trace_from(_capable_events()),
            containment=containment,
            conformance=conformance,
            agent_profile=ProfileGateResult(
                ok=False, detail="approval mode was 'auto', not 'yolo'"
            ),
            capability_readiness=readiness,
        )
        self.assertEqual(report.verdict, SpikeVerdict.NOT_MEASURABLE)
        self.assertIn("agent profile", report.detail)

    def test_a_breached_run_is_not_measurable_even_if_capable(self) -> None:
        _, conformance = self._clean_reports()
        profile, readiness = self._prerequisites()
        breached = evaluate_containment(
            [
                ProbeResult(
                    probe_id=probe.probe_id,
                    status=(
                        ProbeStatus.BREACHED
                        if probe.probe_id == "docker-socket-absent"
                        else ProbeStatus.CONTAINED
                    ),
                )
                for probe in DEFAULT_CONTAINMENT_PROBES
            ],
            workcell_manifest_digest=_SHA,
        )
        report = build_spike_report(
            pin=_pin(),
            run=_run_record(),
            trace=_trace_from(_capable_events()),
            containment=breached,
            conformance=conformance,
            agent_profile=profile,
            capability_readiness=readiness,
        )
        self.assertEqual(report.verdict, SpikeVerdict.NOT_MEASURABLE)
        self.assertIn("not a valid capability experiment", report.detail)

    def test_a_non_conformant_run_is_not_measurable(self) -> None:
        containment, _ = self._clean_reports()
        profile, readiness = self._prerequisites()
        broken = evaluate_conformance([], workcell_manifest_digest=_SHA)
        report = build_spike_report(
            pin=_pin(),
            run=_run_record(),
            trace=_trace_from(_capable_events()),
            containment=containment,
            conformance=broken,
            agent_profile=profile,
            capability_readiness=readiness,
        )
        self.assertEqual(report.verdict, SpikeVerdict.NOT_MEASURABLE)

    def test_capability_comes_from_behaviour_not_configuration(self) -> None:
        # An empty session configured identically demonstrates nothing.
        observations = observe_capabilities(_trace_from([]), run=_run_record())
        self.assertTrue(
            all(item.status == CapabilityStatus.UNPROVEN for item in observations)
        )

    def test_failed_tool_calls_do_not_count_as_capability(self) -> None:
        events = [
            {"type": "tool.call", "call_id": "c1", "name": "write_file"},
            {"type": "tool.result", "call_id": "c1", "exit_code": 1},
        ]
        observations = {
            item.capability: item.status
            for item in observe_capabilities(_trace_from(events), run=_run_record())
        }
        self.assertEqual(
            observations[BaselineCapability.ORDINARY_FILE_EDITING],
            CapabilityStatus.UNPROVEN,
        )


class ControllerTests(unittest.TestCase):
    def test_create_argv_enforces_every_hardening_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkcellController(_config(Path(tmp)))
            argv = controller.build_create_argv()
            joined = " ".join(argv)
            for flag in (
                "--network none",
                "--cap-drop ALL",
                "--security-opt no-new-privileges",
                "--pull=never",
                "--user 65532:65532",
            ):
                self.assertIn(flag, joined)
            self.assertIn("--pids-limit", argv)
            self.assertIn("--memory", argv)
            self.assertIn("--cpus", argv)

    def test_the_image_is_pinned_by_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkcellController(_config(Path(tmp)))
            self.assertIn("@sha256:", controller.image_reference)
            self.assertIn(controller.image_reference, controller.build_create_argv())

    def test_the_task_artifact_is_mounted_read_only_outside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv = WorkcellController(_config(Path(tmp))).build_create_argv()
            mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "-v"]
            task_mount = next(item for item in mounts if "/task/task.md" in item)
            self.assertTrue(task_mount.endswith(":ro"))
            workspace_mount = next(item for item in mounts if ":/workspace:" in item)
            self.assertTrue(workspace_mount.endswith(":rw"))

    def test_the_model_socket_is_the_only_declared_egress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            argv = WorkcellController(config).build_create_argv()
            self.assertIn("--network", argv)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertTrue(
                any(config.egress.socket_container_directory in item for item in argv)
            )
            self.assertTrue(
                any(item == f"OPENAI_BASE_URL={config.egress.base_url}" for item in argv)
            )

    def test_only_the_dedicated_socket_directory_is_mounted(self) -> None:
        # Never a broad writable host path: that would be a channel the relay
        # does not mediate.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            argv = WorkcellController(config).build_create_argv()
            mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "-v"]
            socket_mount = next(
                item
                for item in mounts
                if config.egress.socket_container_directory in item
            )
            self.assertTrue(socket_mount.startswith(config.egress.socket_host_directory))
            self.assertNotIn(str(Path(tmp)) + ":", socket_mount)

    def test_the_forwarder_is_mounted_read_only_outside_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            argv = WorkcellController(config).build_create_argv()
            mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "-v"]
            forwarder = next(item for item in mounts if "forwarder.py" in item)
            self.assertTrue(forwarder.endswith(":ro"))
            self.assertNotIn(":/workspace", forwarder)

    def test_tooling_inside_the_worktree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            egress = _egress(root)
            payload = egress.model_dump(mode="json")
            payload["forwarder_container_path"] = "/workspace/forwarder.py"
            with self.assertRaises(ValueError):
                EgressPolicy.model_validate(payload)

    def test_preflight_refuses_a_forwarder_whose_hash_is_not_pinned(self) -> None:
        from apoapsis.execution.backend import SandboxUnavailableError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, runtime_executable="true")
            # The pin declares a different hash than the file on disk.
            with self.assertRaises(SandboxUnavailableError):
                WorkcellController(config).preflight()

    def test_the_container_is_a_persistent_shell_host(self) -> None:
        # One container for the whole session, not one per command.
        with tempfile.TemporaryDirectory() as tmp:
            argv = WorkcellController(_config(Path(tmp))).build_create_argv()
            self.assertEqual(argv[-2:], ["sleep", "infinity"])

    def test_cleanup_is_not_clean_when_ownership_is_unverified(self) -> None:
        record = CleanupRecord(
            container_removed=True,
            ownership_verified=False,
            process_tree_terminated=True,
            model_socket_removed=True,
        )
        self.assertFalse(record.clean)

    def test_preflight_reports_a_missing_runtime_rather_than_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(
                Path(tmp), runtime_executable="apoapsis-nonexistent-runtime"
            )
            from apoapsis.execution.backend import SandboxUnavailableError

            with self.assertRaises(SandboxUnavailableError):
                WorkcellController(config).preflight()


class WorkcellCliTests(unittest.TestCase):
    def test_preflight_validates_pins_without_a_runtime(self) -> None:
        from apoapsis.cli.app import _workcell_preflight_command

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, runtime_executable="apoapsis-nonexistent-runtime")
            path = root / "workcell.json"
            path.write_text(
                json.dumps(config.model_dump(mode="json")), encoding="utf-8"
            )
            payload = _workcell_preflight_command(path)
            self.assertEqual(
                payload["workcell_manifest_digest"], config.pin.manifest_digest()
            )
            self.assertFalse(payload["runtime_available"])
            self.assertIn("was not found on PATH", payload["runtime_error"])
            self.assertEqual(payload["network"], "none")

    def test_an_unpinned_configuration_is_refused(self) -> None:
        from apoapsis.cli.app import _workcell_preflight_command

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            payload = config.model_dump(mode="json")
            del payload["pin"]["model"]["server_flags_sha256"]
            path = root / "workcell.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(Exception):
                _workcell_preflight_command(path)


if __name__ == "__main__":
    unittest.main()

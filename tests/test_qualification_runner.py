"""The two R3 runner defects that no test reached, and the guards against them.

R3 was bound as the runner authority on the strength of tests that drove its
*helpers*. Neither `stage_7_accounting` nor `manifest_filename` was ever
called by a test, so both shipped wrong while the suite stayed green:

* `stage_7_accounting` looked up the control arm as `"qwen_default_control"`.
  The manifest, `ArmKind` and `scheduled_slots` all emit
  `"default_qwen_control"`, so every pair returned `comparable=False` and the
  rehearsal could only ever reach `INCOMPARABLE_CONFIGURATION`. The failure
  message even listed both arms it said were missing.
* `manifest_filename` mapped schema `"2.0"` to the literal v2 filename. Every
  manifest from v2 onward is schema 2.0, so under manifest v3 the
  changed-server-argument control would have refused the superseded v2
  document -- the right detector firing against the wrong bytes.

Nothing here starts a container, opens a socket or calls a model.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
import unittest
from pathlib import Path

from apoapsis.qualification import runner as runner_module
from apoapsis.qualification.fake_pilot_provider import ScriptId
from apoapsis.qualification.pilot import PilotLock, PilotManifest
from apoapsis.qualification.rehearsal import (
    REQUIRED_DETECTORS,
    ArmSlotResult,
    EvidenceWriter,
    NegativeControl,
    RehearsalVerdict,
    StageOutcome,
    StageResult,
    TeardownProof,
    decide_verdict,
    scheduled_slots,
)
from apoapsis.qualification.runner import (
    SHAPE_BY_REPETITION,
    manifest_document,
    stage_6_negative_controls,
    stage_7_accounting,
)

QUALIFICATION_DIR_MARKER = "docs/qualification"

REPO = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO / "docs" / "qualification"


def _manifest() -> PilotManifest:
    """The newest committed manifest, so the arm names come from the artifact.

    Deliberately not a literal list typed into this test. The defect was a
    disagreement between the runner and the frozen document; a test that
    invented its own arm names could reproduce the disagreement.
    """

    for name in (
        "slice7-crisis-atlas-pilot-manifest-v5.json",
        "slice7-crisis-atlas-pilot-manifest-v4.json",
        "slice7-crisis-atlas-pilot-manifest-v3.json",
        "slice7-crisis-atlas-pilot-manifest-v2.json",
        "slice7-crisis-atlas-pilot-manifest.json",
    ):
        path = QUALIFICATION / name
        if path.exists():
            return PilotManifest.model_validate_json(path.read_text(encoding="utf-8"))
    raise AssertionError(  # pragma: no cover - the pilot cannot exist without one
        "no pilot manifest is committed"
    )


def _teardown() -> TeardownProof:
    return TeardownProof(
        worktree_removed=True,
        qwen_home_removed=True,
        evidence_retained=True,
        no_surviving_worker=True,
        no_surviving_relay_stream=True,
        next_slot_cannot_reach_previous=True,
    )


def _slots_from_frozen_schedule(manifest: PilotManifest) -> tuple[ArmSlotResult, ...]:
    """Six slots whose arm names are read out of the manifest itself."""

    slots: list[ArmSlotResult] = []
    for repetition, arm, order in scheduled_slots(manifest):
        script = SHAPE_BY_REPETITION[repetition]
        incomplete = script is ScriptId.INCOMPLETE_PROPOSAL
        slots.append(
            ArmSlotResult(
                repetition_id=repetition,
                arm=arm,
                order_within_repetition=order,
                script=script,
                seed_commit_verified=True,
                task_bytes_verified=True,
                arm_visible_mounts_verified=True,
                evaluator_only_absent=True,
                provider_requests=1,
                relay_observed_requests=1,
                checkpoint_outcome="CONTINUE" if incomplete else "COMPLETE",
                readiness_blocks=("missing-artifact",) if incomplete else (),
                satisfied_criteria=("criterion-a", "criterion-b"),
                teardown=_teardown(),
                evidence_path=f"/evidence/{repetition}/{arm}",
            )
        )
    return tuple(slots)


class PairScoringUsesTheScheduledArmNames(unittest.TestCase):
    """The regression that made `PASS_LIVE_PREFLIGHT_AUTHORIZED` unreachable."""

    def setUp(self) -> None:
        self.manifest = _manifest()
        self.slots = _slots_from_frozen_schedule(self.manifest)
        self.writer = EvidenceWriter(Path(tempfile.mkdtemp(prefix="runner-test-")))

    def test_every_pair_is_comparable_and_populated(self) -> None:
        _, _, pairs = stage_7_accounting(self.slots, writer=self.writer)

        self.assertEqual(len(pairs), 3)
        for pair in pairs:
            with self.subTest(repetition=pair.repetition_id):
                self.assertTrue(pair.comparable, pair.incomparable_reason)
                self.assertIsNone(pair.incomparable_reason)
                # `regressed` is False when a score is None, so an unpopulated
                # pair reads exactly like a pair that did not regress. That is
                # why absence is asserted against here rather than trusted.
                self.assertIsNotNone(pair.control_proposal_quality)
                self.assertIsNotNone(pair.sandbox_proposal_quality)
                self.assertIsNotNone(pair.sandbox_detection_quality)

    def test_the_verdict_these_pairs_support_is_pass(self) -> None:
        _, accounting, pairs = stage_7_accounting(self.slots, writer=self.writer)

        verdict, reason = decide_verdict(
            stages=(StageResult(stage="s", outcome=StageOutcome.PASSED, detail="d"),),
            arm_slots=self.slots,
            negative_controls=(),
            relay_stress_passed=True,
            token_accounting=accounting,
            pair_scores=pairs,
        )

        self.assertIs(verdict, RehearsalVerdict.PASS_LIVE_PREFLIGHT_AUTHORIZED, reason)

    def test_the_control_arm_key_is_not_a_literal(self) -> None:
        """The two names must be unable to drift apart again."""

        # Comments are excluded on purpose: the comment above the lookup names
        # the wrong key deliberately, to record what the defect was. What must
        # not contain it is the executed code.
        source = inspect.getsource(stage_7_accounting)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("qwen_default_control", code)
        self.assertIn("ArmKind.DEFAULT_QWEN_CONTROL", code)


class ControlsReadTheManifestUnderRehearsal(unittest.TestCase):
    """Control 3 must mutate the bytes actually being rehearsed."""

    def test_the_document_is_the_path_the_caller_named(self) -> None:
        path = Path("/somewhere/slice7-crisis-atlas-pilot-manifest-v3.json")
        self.assertEqual(manifest_document(path), path)

    def test_no_manifest_filename_is_inferred_from_a_schema_version(self) -> None:
        source = Path(runner_module.__file__).read_text(encoding="utf-8")
        offenders = [
            line
            for line in source.splitlines()
            if "slice7-crisis-atlas-pilot-manifest" in line
        ]
        self.assertEqual(offenders, [], "the runner names a manifest file by hand")

    def test_stage_6_requires_the_manifest_path(self) -> None:
        signature = inspect.signature(stage_6_negative_controls)
        parameter = signature.parameters.get("manifest_path")
        self.assertIsNotNone(parameter, "stage 6 does not take a manifest path")
        self.assertIs(parameter.default, inspect.Parameter.empty)


class SharedSessionLifecycleTests(unittest.TestCase):
    """The shared Stage 2/3 session must be entered, reused, and exited once.

    `LiveWorkcellSession(config)` allocates a container name and starts nothing.
    R6 stopped there: every Stage 2 probe reached a daemon that had never heard
    of the container, and Stage 3 had no forwarder to find. Both stages then
    reported against a box that did not exist.

    These drive `run_rehearsal` with a recording stub, so the lifecycle is
    asserted from the outside without a container, a model or a network.
    """

    class _Recorder:
        """A stand-in session that records how it was used."""

        def __init__(self, events: list[str], *, fail_on_enter: bool = False) -> None:
            self.events = events
            self.fail_on_enter = fail_on_enter
            self.entered = False

        def __enter__(self):
            self.events.append("enter")
            if self.fail_on_enter:
                raise RuntimeError("the container could not be started")
            self.entered = True
            return self

        def __exit__(self, *_exc) -> None:
            self.events.append("exit")
            self.entered = False

        def run_containment(self):
            self.events.append("stage-2")
            if not self.entered:
                raise AssertionError("stage 2 used a session that was not entered")
            from apoapsis.workcell.containment import (
                DEFAULT_CONTAINMENT_PROBES,
                ProbeResult,
                ProbeStatus,
                evaluate_containment,
            )

            # No container here, so every probe is honestly unproven. The point
            # of this stub is the lifecycle, and a stub that reported
            # containment would be the very defect under test.
            return evaluate_containment(
                [
                    ProbeResult(
                        probe_id=probe.probe_id,
                        status=ProbeStatus.UNPROVEN,
                        exit_code=1,
                        stderr="Error response from daemon: No such container: stub",
                    )
                    for probe in DEFAULT_CONTAINMENT_PROBES
                ],
                workcell_manifest_digest="b" * 64,
                probes=DEFAULT_CONTAINMENT_PROBES,
            )

        def exec(self, argv, timeout_seconds=300.0):
            """Stand in for `docker exec` against a container that is not there.

            The stub answers exactly as the daemon does, so the mount
            observation and the containment classifier both see the shape they
            have to handle rather than a convenient one.
            """

            self.events.append("exec")
            return 1, "", "Error response from daemon: No such container: stub"

        def run_readiness(self):
            self.events.append("stage-3")
            if not self.entered:
                raise AssertionError("stage 3 used a session that was not entered")

            class _NotReady:
                ready = False

                def model_dump(self, **_kwargs):
                    return {"ready": False, "detail": "stub session, no relay"}

            return _NotReady()

    def _run(self, session, *, stage_0_passed: bool = True):
        """Call the real lifecycle owner directly.

        `run_shared_session_stages` is what `run_rehearsal` uses, so this is the
        production path and not a re-implementation of it. Driving the whole
        rehearsal to reach it would execute six real container slots behind it.
        """

        from apoapsis.qualification.runner import run_shared_session_stages

        scratch = Path(tempfile.mkdtemp(prefix="lifecycle-"))
        stages, _iterations, _passed = run_shared_session_stages(
            self.manifest,
            repo=REPO,
            seed=self.seed,
            scratch=scratch,
            writer=EvidenceWriter(scratch / "evidence"),
            session_factory=lambda: session,
            upstream_base_url="http://127.0.0.1:8080",
            relay_iterations=1,
            stage_0_passed=stage_0_passed,
        )
        return stages

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")
        self.seed = NegativeControlsAreExecutedNotDescribedTests._seed()
        if self.seed is None:
            self.skipTest("the Crisis Atlas seed is not present")
        self.manifest = _manifest()

    def test_the_session_is_entered_before_stage_two_and_exited_after(self) -> None:
        events: list[str] = []
        session = self._Recorder(events)

        self._run(session)

        self.assertIn("enter", events, "the session was never entered")
        self.assertIn("stage-2", events, "stage 2 never used the session")
        self.assertLess(
            events.index("enter"),
            events.index("stage-2"),
            "stage 2 ran before the session was entered",
        )
        self.assertEqual(events.count("enter"), 1)
        self.assertEqual(events.count("exit"), 1, "the session was not exited once")
        self.assertEqual(events[-1], "exit", "the session outlived the stages")
        self.assertFalse(session.entered)

    def test_stages_two_and_three_share_one_entered_session(self) -> None:
        events: list[str] = []
        session = self._Recorder(events)

        self._run(session)

        self.assertEqual(events.count("enter"), 1)
        # Both stages ran against the same object between one enter and one
        # exit. Re-entering for stage 3 would give it a different container from
        # the one stage 2 probed.
        self.assertEqual(events[0], "enter")
        self.assertEqual(events[-1], "exit")
        self.assertIn("stage-3", events)

    def test_the_session_is_exited_even_when_a_stage_raises(self) -> None:
        """`ExitStack` owns the exit, so an exception cannot skip teardown."""

        events: list[str] = []

        class _Exploding(self._Recorder):
            def run_containment(self):
                self.events.append("stage-2")
                raise KeyboardInterrupt("something violent mid-stage")

        session = _Exploding(events)
        with self.assertRaises(KeyboardInterrupt):
            self._run(session)

        self.assertEqual(events.count("exit"), 1, "teardown was skipped")
        self.assertEqual(events[-1], "exit")

    def test_a_session_that_cannot_be_entered_leaves_the_stages_unrun(self) -> None:
        """Start failure is an absent measurement, never containment."""

        events: list[str] = []
        stages = self._run(self._Recorder(events, fail_on_enter=True))

        self.assertIn("enter", events)
        # Nothing was entered, so nothing may be exited -- and nothing may be
        # reported as contained either.
        self.assertNotIn("exit", events)
        by_stage = {item.stage: item for item in stages}
        for stage in ("stage-2-containment", "stage-3-relay-stability"):
            with self.subTest(stage=stage):
                self.assertIn(
                    by_stage[stage].outcome,
                    {StageOutcome.UNRUN, StageOutcome.INCONCLUSIVE},
                )
        self.assertIsNot(
            by_stage["stage-2-containment"].outcome, StageOutcome.PASSED
        )


class ContainmentEvidenceTests(unittest.TestCase):
    """An unexecutable probe proves nothing, and must never read as contained."""

    def _probe(self):
        from apoapsis.workcell.containment import DEFAULT_CONTAINMENT_PROBES

        return DEFAULT_CONTAINMENT_PROBES[0]

    def test_a_nonexistent_container_yields_unproven_probes(self) -> None:
        from apoapsis.workcell.containment import ProbeStatus, classify_probe

        for stderr in (
            "Error response from daemon: No such container: apoapsis-workcell-6eb9a26",
            "Error response from daemon: Container abc is not running",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        ):
            with self.subTest(stderr=stderr[:40]):
                # Exit 1 is exactly what a *successfully refused* command gives,
                # which is why the exit code cannot be the discriminator.
                result = classify_probe(
                    self._probe(), exit_code=1, stdout="", stderr=stderr
                )
                self.assertIs(result.status, ProbeStatus.UNPROVEN)

    def test_a_real_refusal_inside_a_running_container_is_contained(self) -> None:
        from apoapsis.workcell.containment import ProbeStatus, classify_probe

        result = classify_probe(self._probe(), exit_code=1, stdout="", stderr="")
        self.assertIs(result.status, ProbeStatus.CONTAINED)

    def test_zero_executed_probes_can_never_pass(self) -> None:
        from apoapsis.workcell.containment import (
            DEFAULT_CONTAINMENT_PROBES,
            ProbeResult,
            ProbeStatus,
            evaluate_containment,
        )

        report = evaluate_containment(
            [
                ProbeResult(
                    probe_id=probe.probe_id,
                    status=ProbeStatus.UNPROVEN,
                    exit_code=1,
                    stderr="Error response from daemon: No such container: x",
                )
                for probe in DEFAULT_CONTAINMENT_PROBES
            ],
            workcell_manifest_digest="a" * 64,
            probes=DEFAULT_CONTAINMENT_PROBES,
        )

        self.assertFalse(report.contained)
        self.assertEqual(len(report.unproven), len(DEFAULT_CONTAINMENT_PROBES))
        self.assertIn("none of the", report.detail)

    def test_a_breach_is_not_read_as_containment_by_the_runner(self) -> None:
        """The runner compared `status.value` against "breach"; it is
        "breached", so the comparison was never true and a real breach would
        have reached PASS."""

        from apoapsis.qualification import runner as runner_module

        source = inspect.getsource(runner_module.stage_2_containment)
        self.assertNotIn('== "breach"', source)
        self.assertIn("report.breaches", source)


class RealContainmentAgainstARunningWorkcellTests(unittest.TestCase):
    """The other half: a genuine `--network none` container still passes.

    Classifying daemon errors as unproven is only a fix if a real, running,
    correctly-contained workcell still records its refusals as containment. This
    starts one. It is skipped -- loudly -- when Docker or the workcell image is
    absent, because a container that never started proves nothing here either.
    """

    def setUp(self) -> None:
        import os

        if shutil.which("docker") is None:
            self.skipTest("docker is not available")
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            # The relay socket has to be given the workcell's group, which is a
            # privileged operation. Skipping is honest; running as a user who
            # cannot chown would fail for a reason that has nothing to do with
            # containment.
            self.skipTest("a real workcell session requires root to own the socket")
        self.seed = NegativeControlsAreExecutedNotDescribedTests._seed()
        if self.seed is None:
            self.skipTest("the Crisis Atlas seed is not present")
        self.manifest = _manifest()
        import subprocess

        probe = subprocess.run(
            ["docker", "image", "inspect", self.manifest.qwen.image],
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest(f"{self.manifest.qwen.image} is not present to this daemon")

    def test_a_running_network_none_container_records_containment(self) -> None:
        from apoapsis.qualification.session_factory import session_factory_from_manifest
        from apoapsis.workcell.containment import ProbeStatus

        scratch = Path(tempfile.mkdtemp(prefix="real-containment-"))
        workspace = scratch / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        session = session_factory_from_manifest(
            self.manifest,
            repo=REPO,
            workspace=workspace,
            socket_directory=scratch / "sockets",
            upstream_base_url="http://127.0.0.1:8080",
        )
        with session:
            report = session.run_containment()

        executed = [
            item
            for item in report.results
            if item.status in {ProbeStatus.CONTAINED, ProbeStatus.BREACHED}
        ]
        self.assertTrue(executed, "no probe executed against the running container")
        # The egress probes are the point: a real command, really refused,
        # inside a real `--network none` namespace.
        by_id = {item.probe_id: item for item in report.results}
        for probe_id in ("no-external-route", "no-default-route"):
            with self.subTest(probe=probe_id):
                self.assertIs(by_id[probe_id].status, ProbeStatus.CONTAINED)
        self.assertEqual(
            [item.probe_id for item in report.results if item.status is ProbeStatus.UNPROVEN],
            [],
            "a running container still produced unproven probes",
        )


class NegativeControlsAreExecutedNotDescribedTests(unittest.TestCase):
    """Stage 6 must actually run. Reading it is not enough.

    R3 and R4 shipped a control 14 -- orchestration-only evidence offered as
    qualification evidence, the control this pilot is named for -- that raised
    `TypeError` the first time it was ever executed: it passed a resolved
    package where a package root was wanted, passed the probe class instead of
    an instance, and omitted `workspace` entirely. No test called
    `stage_6_negative_controls`, so three signature errors in the same call sat
    behind a stage that read as implemented.

    This test needs the Crisis Atlas seed to clone, which is an evaluation
    fixture rather than repository content, so it skips rather than fails when
    the seed is not present -- and says so, so a skip is not read as a pass.
    """

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required to clone the seed")
        self.seed = self._seed()
        if self.seed is None:
            self.skipTest(
                "the Crisis Atlas seed is not present; stage 6 clones it and "
                "cannot substitute anything for it"
            )
        self.manifest = _manifest()
        lock_path = QUALIFICATION / "slice7-crisis-atlas-pilot-lock-v5.json"
        if not lock_path.exists():
            self.skipTest("no lock yet; this is the manifest commit")
        self.lock = PilotLock.model_validate_json(
            lock_path.read_text(encoding="utf-8")
        )
        self.manifest_path = (
            QUALIFICATION / "slice7-crisis-atlas-pilot-manifest-v5.json"
        )

    @staticmethod
    def _seed():
        for candidate in (
            REPO / ".apoapsis-eval" / "slice-e-crisis-atlas-seed-2026-07-29",
            Path("/root/crisis-atlas-seed"),
        ):
            if (candidate / ".git").is_dir():
                return candidate
        return None

    def test_control_fourteen_is_injected_and_caught(self) -> None:
        writer = EvidenceWriter(Path(tempfile.mkdtemp(prefix="stage-6-")))
        stage, controls = stage_6_negative_controls(
            self.manifest,
            self.lock,
            repo=REPO,
            writer=writer,
            manifest_path=self.manifest_path,
            seed_repository=self.seed,
        )

        self.assertIs(stage.outcome, StageOutcome.PASSED, stage.detail)
        self.assertEqual(len(controls), len(REQUIRED_DETECTORS))
        by_control = {item.control: item for item in controls}
        fake = by_control[NegativeControl.FAKE_EVIDENCE_AS_REAL_QUALIFICATION]
        self.assertTrue(fake.refused)
        self.assertEqual(fake.detector_fired, "CasePackageValidation.registerable")
        for item in controls:
            with self.subTest(control=str(item.control)):
                self.assertTrue(item.correctly_detected, item.model_dump(mode="json"))


class RelayFaultsAreInjectedAgainstARealRelayTests(unittest.TestCase):
    """Stage 3's fault machinery must be executable, not merely bound.

    `relay_faults.py` was bound as authority from v3 onward and no test had
    ever called it. It configured its relay with `["POST /v1/chat/completions"]`
    -- the pin's "METHOD PATH" form -- where `ModelRelayConfig` narrows by path
    and rejects anything else, so the first injection raised a validation error
    and took the whole rehearsal down with it. A bound module nothing runs is
    bound in name only.

    Loopback Unix sockets only; no container, no model, no external network.
    """

    def test_all_five_faults_are_injected_and_recorded(self) -> None:
        """Every fault must reach the relay and be recorded as a request."""

        from apoapsis.qualification.relay_faults import run_all_relay_faults

        sockets = Path(tempfile.mkdtemp(prefix="relay-faults-")) / "sockets"
        report = run_all_relay_faults(socket_directory=sockets)

        self.assertGreaterEqual(len(report.outcomes), 5)
        for outcome in report.outcomes:
            with self.subTest(fault=str(outcome.fault)):
                self.assertIsNone(outcome.error, outcome.error)
                self.assertTrue(outcome.relay_responded)
                self.assertTrue(outcome.recorded)
                self.assertTrue(outcome.no_worker_leaked)

    def test_no_fault_is_reported_to_the_client_as_a_complete_answer(self) -> None:
        """The defect this revision closes, as the property it violated.

        Before R6 the relay converted two of the five faults into apparent
        successes: `upstream_disconnect` and `dropped_stream` each returned
        HTTP 200 with a truncated body, no recorded cancellation and
        `upstream_failures` at 0. This was an `expectedFailure` for exactly as
        long as that was true.
        """

        from apoapsis.qualification.relay_faults import run_all_relay_faults

        sockets = Path(tempfile.mkdtemp(prefix="relay-faults-")) / "sockets"
        report = run_all_relay_faults(socket_directory=sockets)
        self.assertTrue(report.all_handled, list(report.unhandled))

    def test_the_two_truncating_faults_are_recorded_as_upstream_failures(self) -> None:
        """Each of the two, individually, and by the right name.

        `all_handled` is a conjunction and would be satisfied by the relay
        refusing everything for some unrelated reason. These assert the specific
        observations: an upstream failure counted, a terminal absent, the
        response marked incomplete -- and, for the stream, that the failure is
        *not* also booked as a cancellation, which would blame the reader for
        the upstream's behaviour.
        """

        from apoapsis.qualification.relay_faults import (
            RelayFault,
            run_relay_fault,
        )

        for fault, expects_terminal_question in (
            (RelayFault.UPSTREAM_DISCONNECT, False),
            (RelayFault.DROPPED_STREAM, True),
        ):
            with self.subTest(fault=str(fault)):
                sockets = Path(tempfile.mkdtemp(prefix="relay-fault-")) / "sockets"
                outcome = run_relay_fault(fault, socket_directory=sockets)

                self.assertIsNone(outcome.error)
                # 1. the upstream failure is recorded, and by that name
                self.assertEqual(outcome.upstream_failures, 1)
                # 2. terminal completion is absent
                self.assertEqual(outcome.incomplete_responses, 1)
                if expects_terminal_question:
                    self.assertIs(outcome.terminal_observed, False)
                # 3. the result is incomplete rather than a success
                self.assertFalse(outcome.response_complete_recorded)
                self.assertTrue(outcome.not_reported_as_success)
                # 4. and the upstream is not blamed on the reader
                self.assertEqual(outcome.cancellations, 0)

    def test_a_well_formed_stream_is_still_recorded_complete(self) -> None:
        """Normal streaming must be unaffected.

        The cheapest way to satisfy every assertion above would be a relay that
        calls everything incomplete. This is the other half of the property, and
        it uses the same terminal the fault cases lack.
        """

        import http.client
        import socket as socket_module
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from apoapsis.workcell.relay import ModelRelay
        from apoapsis.workcell.relay_policy import ModelRelayConfig

        class _GoodUpstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"choices":[{"delta":{"content":"all"},"index":0}]}\n\n'
                )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), _GoodUpstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sockets = Path(tempfile.mkdtemp(prefix="relay-good-")) / "sockets"
        sockets.mkdir(parents=True, exist_ok=True)
        host, port = server.server_address[:2]
        relay = ModelRelay(
            ModelRelayConfig(
                upstream_base_url=f"http://{host}:{port}",
                socket_path=str(sockets / "model.sock"),
                allowed_routes=["/v1/chat/completions"],
            )
        )
        relay.start()
        try:
            connection = http.client.HTTPConnection("localhost", timeout=15)
            sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            sock.settimeout(15)
            sock.connect(str(sockets / "model.sock"))
            connection.sock = sock
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=b'{"model":"probe","messages":[],"stream":true}',
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = response.read()
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn(b"[DONE]", body)
            self.assertTrue(relay.wait_for_records(1))

            record = relay.stats.records[-1]
            self.assertTrue(record.response_complete)
            self.assertIs(record.terminal_observed, True)
            self.assertIsNone(record.rejection)
            self.assertEqual(relay.stats.upstream_failures, 0)
            self.assertEqual(relay.stats.incomplete_responses, 0)
            self.assertTrue(relay.stats.every_response_complete)
        finally:
            relay.stop()
            server.shutdown()
            server.server_close()

    def test_a_slot_with_an_incomplete_response_produces_no_candidate(self) -> None:
        """The controller-side half: an incomplete turn cannot be scored.

        `execute_slot` consults the relay before anything reads the worktree, so
        a truncated stream ends the slot with an error rather than handing files
        to the checkpoint. Asserted against the real wiring -- the session's own
        accessor and the slot's error field -- rather than by starting a
        container, which this test has no need of.
        """

        from apoapsis.qualification import slot_driver

        source = inspect.getsource(slot_driver.execute_slot)
        self.assertIn("incomplete_relay_responses", source)
        self.assertIn("observation.error", source)

        class _Stub:
            def __init__(self, records):
                self._records = records

            def incomplete_relay_responses(self):
                return self._records

        from apoapsis.workcell.live_session import LiveWorkcellSession

        self.assertTrue(hasattr(LiveWorkcellSession, "incomplete_relay_responses"))
        stub = _Stub(("POST /v1/chat/completions: the event stream ended",))
        self.assertEqual(len(stub.incomplete_relay_responses()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

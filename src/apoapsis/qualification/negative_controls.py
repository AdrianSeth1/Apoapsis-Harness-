"""Seventeen injected faults, each answered by the production detector.

The rule this module exists to enforce, stated once and applied without
exception:

    An injected control passes only when the production detector returns its
    exact expected finding. A setup exception fails Stage 6. It never counts
    as detection.

That rule is structural here, not a convention. `_inject` calls the control,
and any exception escaping it becomes a `ControlSetupError` that fails the
stage. The alternative -- `try: ...  except Exception: caught = True` -- is how
the previous two versions of Stage 6 recorded a broken runner as a working
detector, twice, in different clothes. A control that cannot even be set up has
measured nothing.

The second rule follows from the first: a detector that fires for the *wrong*
reason has not caught the fault. Several of these faults trip more than one
check, so each control names the exact finding it requires and
`NegativeControlResult.correctly_detected` compares against it. "Something was
rejected" is not evidence that the mapped detector works, and a control that
accepted it would keep passing after its detector was deleted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from apoapsis.qualification.rehearsal import (
    REQUIRED_DETECTORS,
    NegativeControl,
    NegativeControlResult,
)


class ControlSetupError(RuntimeError):
    """Raised when a control could not be set up. Fails Stage 6; never detects."""


def _inject(
    control: NegativeControl, body: Callable[[], tuple[str | None, bool]]
) -> NegativeControlResult:
    """Run one control. Any escaping exception is a setup failure, not a catch."""

    try:
        fired, refused = body()
    except Exception as exc:  # noqa: BLE001
        raise ControlSetupError(
            f"{control}: setup failed before the detector was consulted "
            f"({type(exc).__name__}: {exc}). This is a defect in the rehearsal, "
            "and it must not be recorded as the detector firing."
        ) from exc
    return NegativeControlResult(
        control=control,
        required_detector=REQUIRED_DETECTORS[control],
        detector_fired=fired,
        refused=refused,
    )


# --------------------------------------------------------------------------
# Individual controls
# --------------------------------------------------------------------------


def _manifest_lock_mismatch(manifest, lock) -> tuple[str | None, bool]:
    from apoapsis.qualification.pilot import authorize_rehearsal

    tampered = lock.model_copy(update={"manifest_digest": "a" * 64})
    decision = authorize_rehearsal(manifest, tampered)
    return ("PilotLock.verify_against" if not decision.authorized else None,
            not decision.authorized)


def _workcell_profile_mismatch(manifest, observed_tools: tuple[str, ...]):
    """Drop one realised tool from *real* Stage 1 evidence and re-gate it."""

    from apoapsis.workcell.agent_profile import (
        REQUIRED_APPROVAL_MODE,
        AgentBinaryIdentity,
        AgentExecutionProfile,
        AgentProfileEvidence,
        ExpectedAgentProfile,
        ProfileFinding,
        evaluate_agent_profile,
    )

    from apoapsis.workcell.agent_profile import REQUIRED_SHELL_TOOLS

    # The gate fires when a whole capability category is unregistered, not when
    # any single tool is missing. Dropping one arbitrary tool left every
    # category still represented, so the detector correctly stayed silent and
    # the control was measuring nothing.
    degraded_tools = sorted(set(observed_tools) - set(REQUIRED_SHELL_TOOLS))
    if degraded_tools == sorted(set(observed_tools)):
        raise ControlSetupError(
            "Stage 1 observed no shell tool to remove, so the realised-tool "
            f"mismatch cannot be injected (observed: {sorted(observed_tools)})"
        )
    if not degraded_tools:
        raise ControlSetupError("removing the shell category left no tools at all")

    qwen = manifest.qwen
    expected = ExpectedAgentProfile(
        package_name=qwen.package_name,
        package_version=qwen.package_version,
        executable_sha256=qwen.entry_point_sha256,
        package_manifest_sha256=qwen.package_metadata_sha256,
        declared_entrypoint=Path(qwen.entry_point_path).name,
        settings_sha256=qwen.effective_settings_sha256,
    )
    # Real evidence, minus one tool. Everything else is exactly what Stage 1saw.
    degraded = AgentProfileEvidence(
        arm="apoapsis_sandbox",
        binary=AgentBinaryIdentity(
            resolved_path=qwen.entry_point_path,
            real_path=qwen.entry_point_path,
            executable_sha256=qwen.entry_point_sha256,
            package_name=qwen.package_name,
            package_version=qwen.package_version,
            package_manifest_sha256=qwen.package_metadata_sha256,
            declared_entrypoint=Path(qwen.entry_point_path).name,
        ),
        profile=AgentExecutionProfile(
            resolved_approval_mode=REQUIRED_APPROVAL_MODE,
            effective_settings_sha256=qwen.effective_settings_sha256,
            realised_tools=degraded_tools,
            cli_version=qwen.package_version,
            event_dialect="stream-json",
        ),
    )
    result = evaluate_agent_profile(degraded, expected, readiness_proven=True)
    fired = ProfileFinding.MISSING_NATIVE_TOOLS in result.findings
    return (
        "StopCondition.CODING_PROFILE_OR_REALISED_TOOLS_DIFFER" if fired else None,
        fired,
    )


def _controlled_variable_mismatch(manifest) -> tuple[str | None, bool]:
    """Two arm records differing in one field, judged by the real `check_pair`."""

    from apoapsis.qualification.manifest import (
        ControlledVariables,
        PairComparability,
        check_pair,
    )

    digest = "b" * 64
    common = dict(
        model_name="qwen",
        model_file_sha256=digest,
        quantization="Q4_K_M",
        llama_server_binary_sha256=digest,
        llama_server_argv_sha256=digest,
        threshold_ladder_sha256=digest,
        auto_compact_trigger_tokens=manifest.budgets.max_output_tokens,
        context_limit_tokens=manifest.threshold_ladder.effective_window_tokens,
        max_output_tokens=manifest.budgets.max_output_tokens,
        qwen_package_sha256=manifest.qwen.package_metadata_sha256,
        qwen_settings_sha256=manifest.qwen.effective_settings_sha256,
        tool_schema_sha256=manifest.qwen.expected_tool_schema_sha256 or digest,
        system_prompt_sha256=digest,
        task_prompt_sha256=digest,
        workcell_image_digest=manifest.workcell_image.image_id,
        verifier_image_digest=manifest.controller_image.image_id,
        controller_source_commit="0" * 40,
        worktree_seed_sha256=digest,
        network_policy="none",
        mount_policy_sha256=digest,
        cpu_limit=4.0,
        gpu_allocation="none",
        per_call_budget_tokens=manifest.budgets.per_provider_call_output_cap,
        per_case_budget_tokens=manifest.budgets.total_token_budget,
        verification_config_sha256=digest,
        repair_policy_sha256=digest,
        cold_start=True,
    )
    control = ControlledVariables(**common)
    # Exactly one field differs, so the refusal must name that field and no
    # other. A pair refused for a field nobody changed would mean the detector
    # is reporting something other than the injected fault.
    sandbox = ControlledVariables(**{**common, "max_output_tokens": common["max_output_tokens"] - 1})
    result = check_pair(control, sandbox)
    fired = (
        result.comparability is PairComparability.INCOMPARABLE
        and tuple(result.mismatched_fields) == ("max_output_tokens",)
    )
    return (
        "StopCondition.REPETITION_CONFIGURATION_DIFFERS" if fired else None,
        fired,
    )


def _missing_durable_evidence(writer) -> tuple[str | None, bool]:
    """Evidence written inside the container must not survive as evidence.

    The gate is `EvidenceWriter.digest()`: evidence the controller never
    received cannot contribute to it. Deleting a file this function created
    would only prove that deletion works.
    """

    from apoapsis.qualification.rehearsal import EvidenceWriter

    container_only = Path(tempfile.mkdtemp(prefix="workcell-scratch-"))
    (container_only / "witness.json").write_text('{"passed": true}', encoding="utf-8")

    probe_root = Path(tempfile.mkdtemp(prefix="durability-probe-"))
    probe_writer = EvidenceWriter(probe_root)
    before = probe_writer.digest()

    # The container is torn down; nothing it wrote reaches the evidence root.
    shutil.rmtree(container_only, ignore_errors=True)
    after = probe_writer.digest()

    fired = before == after and not container_only.exists()
    shutil.rmtree(probe_root, ignore_errors=True)
    return (
        "StopCondition.EVIDENCE_STORAGE_NOT_DURABLE" if fired else None,
        fired,
    )


def _evaluator_only_exposed(package) -> tuple[str | None, bool]:
    """Only the package's own refusal type counts."""

    from apoapsis.qualification.case_package import CasePackageError

    oracle = Path(package.package_root) / "evaluator-only" / "oracle.json"
    try:
        package.assert_arm_visible_set_is_contained((str(oracle),))
    except CasePackageError:
        # The specific refusal. A broad `except Exception` here would launder
        # a TypeError from a changed signature into a passing control.
        return ("ResolvedCasePackage.assert_arm_visible_set_is_contained", True)
    return (None, False)


def _stale_witness_digest() -> tuple[str | None, bool]:
    """Prove the witness is clean at fingerprint A before staling it at B."""

    from apoapsis.workcell.witness import (
        EvidenceClass,
        StructuredWitness,
        WitnessKind,
        WitnessProblem,
        validate_witness,
    )

    fingerprint_a = "a" * 64
    fingerprint_b = "b" * 64
    witness = StructuredWitness(
        witness_id="stale-witness-control",
        kind=WitnessKind.TEST_SUITE,
        evidence_class=EvidenceClass.INDEPENDENT,
        command_name="unit-tests",
        command_version="1",
        command_argv=["python", "-m", "pytest"],
        worktree_fingerprint=fingerprint_a,
        passed=True,
        coverage=_coverage_fixture(),
        criteria_proved=["c-1"],
        artifact_sha256={"coverage.json": "c" * 64},
    )

    # Step one: the witness must be accepted at its own fingerprint. Without
    # this, an unrelated rejection could mask the staleness check and the
    # control would still read as caught.
    baseline = validate_witness(witness, current_fingerprint=fingerprint_a)
    if baseline:
        raise ControlSetupError(
            "the fixture is rejected before the fault is injected: "
            f"{[item.problem for item in baseline]}. An unrelated rejection "
            "would mask the staleness finding this control exists to prove."
        )

    # Step two: the same unchanged witness, judged against a different tree.
    rejections = validate_witness(witness, current_fingerprint=fingerprint_b)
    problems = [item.problem for item in rejections]
    fired = problems == [WitnessProblem.STALE_FINGERPRINT]
    return ("validate_witness" if fired else None, fired)


def _coverage_fixture():
    from apoapsis.workcell.witness import CoverageObservation

    return CoverageObservation(
        collection_method="stdlib trace module",
        executed_paths=["module.py"],
        executed_lines={"module.py": [1, 2]},
        imported_modules=["module"],
    )


def _unmapped_obligation(package) -> tuple[str | None, bool]:
    """A real package copy whose one obligation names no criteria."""

    from apoapsis.qualification.case_package import (
        CasePackageError,
        resolve_case_package,
        validate_criteria_mapping,
    )

    scratch = Path(tempfile.mkdtemp(prefix="unmapped-obligation-"))
    try:
        copy_root = scratch / "package"
        shutil.copytree(Path(package.package_root), copy_root)

        contract_path = copy_root / "plan-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        if not contract["obligations"]:
            raise ControlSetupError("the package declares no obligations to unmap")
        contract["obligations"][0]["criteria"] = []
        contract_path.write_text(
            json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _recompute_artifact_digests(copy_root)

        mutated = resolve_case_package(copy_root)
        try:
            validate_criteria_mapping(mutated)
        except CasePackageError:
            return ("validate_criteria_mapping", True)
        return (None, False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _recompute_artifact_digests(package_root: Path) -> None:
    """Rewrite the manifest's declared digests to match the mutated bytes.

    Without this the package fails on a digest mismatch, which is a real
    refusal for entirely the wrong reason -- the control would look caught
    while `validate_criteria_mapping` was never reached.
    """

    manifest_path = package_root / "package.json"
    if not manifest_path.is_file():
        raise ControlSetupError(f"no package.json under {package_root}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in payload.get("artifacts", []):
        target = package_root / artifact["relative_path"]
        if target.is_file():
            artifact["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _changed_task_bytes(package, manifest) -> tuple[str | None, bool]:
    """Mutate task.md in a real package copy and re-resolve it."""

    from apoapsis.qualification.case_package import CasePackageError, resolve_case_package

    scratch = Path(tempfile.mkdtemp(prefix="changed-task-"))
    try:
        copy_root = scratch / "package"
        shutil.copytree(Path(package.package_root), copy_root)
        task = copy_root / "task.md"
        task.write_bytes(task.read_bytes() + b"\n<injected>\n")
        # Digests are deliberately *not* recomputed: the declared digest is the
        # detector, and this is the fault it exists to catch.
        try:
            resolve_case_package(copy_root)
        except CasePackageError:
            return ("StopCondition.SEED_TASK_OR_CONTRACT_BYTES_DIFFER", True)
        return (None, False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _output_ceiling_truncation(manifest) -> tuple[str | None, bool]:
    from apoapsis.models.ceilings import CeilingStopReason, classify_ceiling_stop_reason

    reason = classify_ceiling_stop_reason(
        finish_reason="length",
        input_tokens=100,
        output_tokens=manifest.budgets.max_output_tokens,
        context_limit=manifest.threshold_ladder.effective_window_tokens,
        max_output_tokens=manifest.budgets.max_output_tokens,
    )
    fired = reason is CeilingStopReason.OUTPUT_CEILING_TRUNCATION
    return ("CeilingStopReason.OUTPUT_CEILING_TRUNCATION" if fired else None, fired)


def _input_context_exhausted(manifest) -> tuple[str | None, bool]:
    from apoapsis.models.ceilings import CeilingStopReason, classify_ceiling_stop_reason

    window = manifest.threshold_ladder.effective_window_tokens
    reason = classify_ceiling_stop_reason(
        finish_reason="length",
        input_tokens=window - 8,
        output_tokens=8,
        context_limit=window,
        max_output_tokens=manifest.budgets.max_output_tokens,
    )
    fired = reason is CeilingStopReason.INPUT_CONTEXT_EXHAUSTED
    return ("CeilingStopReason.INPUT_CONTEXT_EXHAUSTED" if fired else None, fired)


def _unclassified_stop_reason(manifest) -> tuple[str | None, bool]:
    """The `teapot` stop must classify as nothing at all.

    A finish reason the classifier does not recognise has to come back as
    `None`. Returning a ceiling label for an unknown stop would attribute a
    failure to the window that the window did not cause.
    """

    from apoapsis.models.ceilings import classify_ceiling_stop_reason
    from apoapsis.qualification.fake_pilot_provider import SCRIPTS, ScriptId

    turn = SCRIPTS[ScriptId.UNCLASSIFIED_STOP_REASON][0]
    if turn.finish_reason != "teapot":
        raise ControlSetupError(
            f"the unclassified script no longer stops with 'teapot' "
            f"(got {turn.finish_reason!r}), so this control is not injecting "
            "the fault it names"
        )
    reason = classify_ceiling_stop_reason(
        finish_reason=turn.finish_reason,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        context_limit=manifest.threshold_ladder.effective_window_tokens,
        max_output_tokens=manifest.budgets.max_output_tokens,
    )
    fired = reason is None
    return (
        "StopCondition.TELEMETRY_CANNOT_BE_CLASSIFIED" if fired else None,
        fired,
    )


def _repair_entering_proposal_score() -> tuple[str | None, bool]:
    """Build a repaired proposal score and require the model to refuse it."""

    from pydantic import ValidationError

    from apoapsis.qualification.manifest import ProposalScore

    try:
        ProposalScore(
            case_id="crisis-atlas",
            obligations_implemented=3,
            obligations_required=3,
            independent_checks_passed=3,
            repair_applied=True,
        )
    except ValidationError:
        return ("RepairPolicy.repair_may_improve_proposal_score", True)
    return (None, False)


def _clean_slot_fixtures():
    """Six slots that pass every per-slot check.

    Deliberately clean: this fixture exists so the regression control reaches
    the pair-score rule, and any defect planted here would be caught by a
    different check and attributed to the wrong cause.
    """

    from apoapsis.qualification.fake_pilot_provider import ScriptId
    from apoapsis.qualification.rehearsal import ArmSlotResult, TeardownProof

    teardown = TeardownProof(
        worktree_removed=True,
        qwen_home_removed=True,
        evidence_retained=True,
        no_surviving_worker=True,
        no_surviving_relay_stream=True,
        next_slot_cannot_reach_previous=True,
    )
    slots = []
    for index, repetition in enumerate(
        ("crisis-atlas-rep-1", "crisis-atlas-rep-2", "crisis-atlas-rep-3")
    ):
        for order, arm in enumerate(("qwen_default_control", "apoapsis_sandbox")):
            slots.append(
                ArmSlotResult(
                    repetition_id=repetition,
                    arm=arm,
                    order_within_repetition=order + 1,
                    script=ScriptId.COMPLETE_PROPOSAL,
                    seed_commit_verified=True,
                    task_bytes_verified=True,
                    arm_visible_mounts_verified=True,
                    evaluator_only_absent=True,
                    provider_requests=1,
                    relay_observed_requests=1,
                    checkpoint_outcome="COMPLETE",
                    teardown=teardown,
                    evidence_path=f"/evidence/{repetition}-{arm}",
                )
            )
    return tuple(slots)


def _regression_hidden_by_aggregate() -> tuple[str | None, bool]:
    """Three populated pairs; the mean improves but one pair regresses."""

    from apoapsis.qualification.rehearsal import (
        PairScore,
        RehearsalVerdict,
        StageOutcome,
        StageResult,
        TokenAccounting,
        decide_verdict,
    )

    pairs = (
        PairScore(
            repetition_id="crisis-atlas-rep-1",
            control_proposal_quality=0.5,
            sandbox_proposal_quality=0.2,  # the regression
            sandbox_detection_quality=1.0,
        ),
        PairScore(
            repetition_id="crisis-atlas-rep-2",
            control_proposal_quality=0.5,
            sandbox_proposal_quality=0.9,
            sandbox_detection_quality=1.0,
        ),
        PairScore(
            repetition_id="crisis-atlas-rep-3",
            control_proposal_quality=0.5,
            sandbox_proposal_quality=0.9,
            sandbox_detection_quality=1.0,
        ),
    )
    # Mean sandbox 0.667 > mean control 0.5: an aggregate view calls this an
    # improvement. The per-pair rule must still refuse it.
    #
    # Six clean slots are required, because `decide_verdict` checks the slot
    # count first. With `arm_slots=()` it returned NOT_MEASURABLE and the pair
    # scores were never consulted -- the control looked like a miss while the
    # rule it targets was never exercised.
    verdict, reason = decide_verdict(
        stages=(StageResult(stage="s", outcome=StageOutcome.PASSED, detail="d"),),
        arm_slots=_clean_slot_fixtures(),
        negative_controls=(),
        relay_stress_passed=True,
        token_accounting=TokenAccounting(unmeasured_reason="none"),
        pair_scores=pairs,
    )
    fired = (
        verdict is RehearsalVerdict.FAIL_REHEARSAL
        and "crisis-atlas-rep-1" in reason
    )
    return (
        "PairScore.aggregate_may_offset_pair_regression" if fired else None,
        fired,
    )


def _prior_arm_contamination(writer) -> tuple[str | None, bool]:
    """A real observed residue, run through the real `prove_teardown`."""

    from apoapsis.qualification.observation import RuntimeResidue
    from apoapsis.qualification.rehearsal import prove_teardown

    scratch = Path(tempfile.mkdtemp(prefix="contamination-"))
    try:
        worktree = scratch / "worktree"
        worktree.mkdir(parents=True)
        # Left behind on purpose: this is what a slot that failed to tear down
        # actually looks like.
        (worktree / "leftover.txt").write_text("prior arm", encoding="utf-8")
        qwen_home = scratch / "qwen-home"
        evidence = scratch / "evidence"
        evidence.mkdir(parents=True)
        (evidence / "slot.json").write_text("{}", encoding="utf-8")

        residue = RuntimeResidue(
            surviving_containers=("apoapsis-workcell-prior",),
            surviving_relay_streams=1,
            relay_socket_present=True,
        )
        proof = prove_teardown(
            worktree=worktree,
            qwen_home=qwen_home,
            evidence=evidence,
            surviving_workers=len(residue.surviving_containers),
            surviving_relay_streams=residue.surviving_relay_streams,
        )
        fired = not proof.clean
        return ("TeardownProof" if fired else None, fired)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _absent_required_repetition() -> tuple[str | None, bool]:
    from apoapsis.qualification.rehearsal import (
        RehearsalVerdict,
        StageOutcome,
        StageResult,
        TokenAccounting,
        decide_verdict,
    )

    verdict, _ = decide_verdict(
        stages=(StageResult(stage="s", outcome=StageOutcome.PASSED, detail="d"),),
        arm_slots=(),
        negative_controls=(),
        relay_stress_passed=True,
        token_accounting=TokenAccounting(unmeasured_reason="none"),
        pair_scores=(),
    )
    fired = verdict is RehearsalVerdict.NOT_MEASURABLE
    return ("decide_verdict:arm_slot_count" if fired else None, fired)


def _changed_server_argument(manifest, repo: Path, manifest_name: str):
    from pydantic import ValidationError

    from apoapsis.qualification.pilot import PilotManifest

    payload = json.loads(
        (repo / "docs" / "qualification" / manifest_name).read_text(encoding="utf-8")
    )
    argv = list(payload["server"]["argv"])
    if "65536" not in argv:
        raise ControlSetupError(
            "the locked server argv no longer contains the context argument "
            "this control mutates"
        )
    argv[argv.index("65536")] = "32768"
    payload["server"]["argv"] = argv
    try:
        PilotManifest.model_validate(payload)
    except ValidationError:
        return ("ServerIdentity.argv_sha256", True)
    return (None, False)


def _fake_evidence_as_real_qualification(package) -> tuple[str | None, bool]:
    """Orchestration-only evidence must never be registerable."""

    from apoapsis.qualification.case_package import CasePackageValidation, EvidenceKind

    validation = CasePackageValidation(
        package_id=package.package_id,
        package_digest=package.package_digest,
        evidence_kind=EvidenceKind.ORCHESTRATION_ONLY,
        results=(),
    )
    fired = not validation.registerable
    return ("CasePackageValidation.registerable" if fired else None, fired)


def run_negative_controls(
    manifest,
    lock,
    *,
    repo: Path,
    package,
    observed_tools: tuple[str, ...],
    manifest_name: str,
    writer,
) -> tuple[tuple[NegativeControlResult, ...], tuple[str, ...]]:
    """Inject all seventeen. Raises `ControlSetupError` if any setup fails."""

    results = [
        _inject(NegativeControl.MANIFEST_LOCK_MISMATCH,
                lambda: _manifest_lock_mismatch(manifest, lock)),
        _inject(NegativeControl.WORKCELL_PROFILE_MISMATCH,
                lambda: _workcell_profile_mismatch(manifest, observed_tools)),
        _inject(NegativeControl.CONTROLLED_VARIABLE_MISMATCH,
                lambda: _controlled_variable_mismatch(manifest)),
        _inject(NegativeControl.MISSING_DURABLE_EVIDENCE,
                lambda: _missing_durable_evidence(writer)),
        _inject(NegativeControl.EVALUATOR_ONLY_EXPOSED,
                lambda: _evaluator_only_exposed(package)),
        _inject(NegativeControl.STALE_WITNESS_DIGEST, _stale_witness_digest),
        _inject(NegativeControl.UNMAPPED_OBLIGATION,
                lambda: _unmapped_obligation(package)),
        _inject(NegativeControl.OUTPUT_CEILING_TRUNCATION,
                lambda: _output_ceiling_truncation(manifest)),
        _inject(NegativeControl.INPUT_CONTEXT_EXHAUSTED,
                lambda: _input_context_exhausted(manifest)),
        _inject(NegativeControl.PRIOR_ARM_CONTAMINATION,
                lambda: _prior_arm_contamination(writer)),
        _inject(NegativeControl.CHANGED_TASK_BYTES,
                lambda: _changed_task_bytes(package, manifest)),
        _inject(NegativeControl.CHANGED_SERVER_ARGUMENT,
                lambda: _changed_server_argument(manifest, repo, manifest_name)),
        _inject(NegativeControl.UNCLASSIFIED_STOP_REASON,
                lambda: _unclassified_stop_reason(manifest)),
        _inject(NegativeControl.REPAIR_ENTERING_PROPOSAL_SCORE,
                _repair_entering_proposal_score),
        _inject(NegativeControl.REGRESSION_HIDDEN_BY_AGGREGATE,
                _regression_hidden_by_aggregate),
        _inject(NegativeControl.ABSENT_REQUIRED_REPETITION,
                _absent_required_repetition),
        _inject(NegativeControl.FAKE_EVIDENCE_AS_REAL_QUALIFICATION,
                lambda: _fake_evidence_as_real_qualification(package)),
    ]

    injected = {item.control for item in results}
    missing = tuple(
        sorted(str(control) for control in REQUIRED_DETECTORS if control not in injected)
    )
    return tuple(results), missing

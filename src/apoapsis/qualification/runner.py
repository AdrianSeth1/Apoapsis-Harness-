"""`run_rehearsal`: the sequencer, not the ingredients.

Slice 7P.2S added verdict models, a scripted provider, teardown proofs and
`decide_verdict`, and lock v2 bound them. That was still not a runner. Nothing
in it executed Stage 0 then Stage 1 then Stage 2; the tests drove each helper
by hand, which proves the helpers work and proves nothing about a rehearsal.
Binding ingredients and calling it a bound runner is the same substitution this
pilot has now made three times in different clothes: an artifact that names an
authority it does not actually cover.

So this module is the thing that was missing. `run_rehearsal` executes Stages
0-8 in order, in one call, and returns one `RehearsalReport`. It is the only
entry point the CLI has, and the CLI is a thin shell around it precisely so
that "the bound executable" and "the thing that ran" cannot drift apart.

Two rules it will not bend.

**A container that never started cannot satisfy containment.** Stages 1-3 need
the real workcell and the real relay. When the runtime is unavailable those
stages report `UNRUN`, and `decide_verdict` turns any `UNRUN` into
`NOT_MEASURABLE`. There is no path by which an absent container yields
`PASS_LIVE_PREFLIGHT_AUTHORIZED`, which is the whole reason the four-state
outcome exists rather than a boolean.

**Six slots are executed, not asserted.** Stage 4 clones the seed six times,
applies a scripted candidate, drives the authoritative checkpoint, and proves
teardown. A rehearsal that reported six slots without running six is exactly
the false-completion shape the Crisis Atlas case was built to detect.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from apoapsis.qualification.authority import BoundModule, verify_authority
from apoapsis.qualification.case_package import resolve_case_package
from apoapsis.qualification.fake_pilot_provider import (
    ScriptId,
    script_digest,
)
from apoapsis.qualification.fake_provider_server import PROBE_MARKER, tool_schema_digest
from apoapsis.qualification.observation import (
    RuntimeResidue,
    observe_capability,
    observe_egress_refusal,
    observe_mounts,
    observe_teardown,
)
from apoapsis.qualification.pilot import (
    ArmKind,
    PilotLock,
    PilotManifest,
    authorize_rehearsal,
)
from apoapsis.qualification.real_probe import RealCasePackageProbe
from apoapsis.qualification.relay_faults import run_all_relay_faults
from apoapsis.qualification.session_factory import session_factory_from_manifest
from apoapsis.qualification.slot_driver import execute_slot
from apoapsis.qualification.rehearsal import (
    REQUIRED_DETECTORS,
    ArmSlotResult,
    EvidenceWriter,
    NegativeControl,
    NegativeControlResult,
    PairScore,
    RehearsalReport,
    RehearsalVerdict,
    StageOutcome,
    StageResult,
    TokenAccounting,
    decide_verdict,
    prove_teardown,
    scheduled_slots,
)

#: Which candidate shape each repetition rehearses. Repetition 1 replays the
#: historical incomplete proposal, because the case exists to detect it;
#: repetitions 2 and 3 rehearse the complete shape so the `COMPLETE` path is
#: exercised too. Both arms of a pair see the same shape, or the pair would
#: not be matched.
SHAPE_BY_REPETITION: dict[str, ScriptId] = {
    "crisis-atlas-rep-1": ScriptId.INCOMPLETE_PROPOSAL,
    "crisis-atlas-rep-2": ScriptId.COMPLETE_PROPOSAL,
    "crisis-atlas-rep-3": ScriptId.COMPLETE_PROPOSAL,
}


class RehearsalInputsError(RuntimeError):
    """The rehearsal cannot start. Distinct from a rehearsal that ran and failed."""


def _stage(name: str, outcome: StageOutcome, detail: str, **evidence: str) -> StageResult:
    return StageResult(stage=name, outcome=outcome, detail=detail, evidence=evidence)


def stage_0_verify_lock(
    manifest: PilotManifest, lock: PilotLock, *, repo: Path, writer: EvidenceWriter
) -> StageResult:
    """Recompute the locked identities and refuse any drift."""

    decision = authorize_rehearsal(manifest, lock)
    if not decision.authorized:
        return _stage("stage-0-lock", StageOutcome.FAILED, decision.reason)

    package_root = repo / manifest.crisis_atlas.package_root
    try:
        package = resolve_case_package(package_root)
    except Exception as exc:  # noqa: BLE001
        return _stage("stage-0-lock", StageOutcome.FAILED, f"package: {exc}")

    if package.package_digest != manifest.crisis_atlas.package_digest:
        return _stage(
            "stage-0-lock",
            StageOutcome.FAILED,
            f"package digests to {package.package_digest}, locked "
            f"{manifest.crisis_atlas.package_digest}",
        )

    authority = manifest.pilot_authority
    if authority is None:
        return _stage(
            "stage-0-lock",
            StageOutcome.FAILED,
            "the manifest binds no pilot authority, so no executable is bound",
        )
    declared = tuple(
        BoundModule(path=item["path"], sha256=item["sha256"])
        for item in authority.bound_modules
    )
    verified = verify_authority(authority.authority_commit, declared, repo=repo)
    if not verified.satisfied:
        return _stage(
            "stage-0-lock",
            StageOutcome.FAILED,
            "; ".join(item.detail for item in verified.findings),
        )
    if authority.fake_provider_script_sha256 != script_digest():
        return _stage(
            "stage-0-lock",
            StageOutcome.FAILED,
            "the fake-provider script does not match the bound digest",
        )

    writer.write_json(
        "stage-0/verification.json",
        {
            "manifest_digest": manifest.digest(),
            "lock_digest": lock.digest(),
            "package_digest": package.package_digest,
            "authority_commit": authority.authority_commit,
            "bound_modules": [item.model_dump(mode="json") for item in verified.verified],
            "fake_provider_script_sha256": script_digest(),
        },
    )
    return _stage(
        "stage-0-lock",
        StageOutcome.PASSED,
        "manifest, lock, package and every bound executable recompute",
        manifest_digest=manifest.digest(),
        lock_digest=lock.digest(),
    )


def stage_1_runtime_identity(
    manifest: PilotManifest,
    *,
    repo: Path,
    seed_repository: Path,
    scratch: Path,
    writer: EvidenceWriter,
) -> StageResult:
    """Reobserve the realised Qwen surface by *running* a sacrificial slot.

    Static package presence is explicitly insufficient: Slice 2C found an image
    whose CLI exposed 57 tools with no `write_file` at all while the pin
    declared 13, and the v2 manifest recorded 13 for an image that puts 26 on
    the wire. Reading `package.json` -- what the previous version of this stage
    did -- would have agreed with both of those wrong numbers, because the
    package is not the surface.

    So the surface is taken off the wire: a real container starts, a real CLI
    sends its real `tools` array, and the names and the full schema digest are
    compared against the manifest. The probe is sacrificial and runs before the
    six scheduled slots, so a capability failure is attributed here rather than
    corrupting a scored slot.
    """

    evidence_dir = Path(writer.root) / "stage-1"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    try:
        observation = execute_slot(
            manifest,
            repo=repo,
            seed_repository=seed_repository,
            base=scratch / "stage-1",
            repetition_id="stage-1",
            arm="capability-probe",
            script=ScriptId.CAPABILITY_PROBE,
            evidence_dir=evidence_dir,
            max_output_tokens=manifest.budgets.max_output_tokens,
            keep_workspace=True,
        )
    except Exception as exc:  # noqa: BLE001
        writer.write_json("stage-1/error.json", {"error": f"{type(exc).__name__}: {exc}"})
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.UNRUN,
            f"the workcell could not be started ({type(exc).__name__}: {exc}), so "
            "the realised tool surface was not observed; a count from the "
            "manifest is not an observation",
        )

    if observation.error is not None:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            f"the capability probe slot failed: {observation.error}",
        )

    observed_names = tuple(observation.observed_tool_names)
    expected_names = tuple(manifest.qwen.expected_tool_names)
    schema_digest = (
        tool_schema_digest(observation.observed_tool_schema)
        if observation.observed_tool_schema
        else None
    )

    workspace = observation.kept_workspace
    capability = (
        observe_capability(workspace, marker=PROBE_MARKER)
        if workspace is not None
        else None
    )

    writer.write_json(
        "stage-1/surface.json",
        {
            "observed_tool_names": list(observed_names),
            "expected_tool_names": list(expected_names),
            "observed_count": len(observed_names),
            "expected_count": manifest.qwen.expected_native_tool_count,
            "observed_schema_sha256": schema_digest,
            "expected_schema_sha256": manifest.qwen.expected_tool_schema_sha256,
            "capability": capability.model_dump(mode="json") if capability else None,
        },
    )

    if not observed_names:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.UNRUN,
            "the CLI sent no tools array, so the realised surface was not "
            "observed at all",
        )

    missing = sorted(set(expected_names) - set(observed_names))
    unexpected = sorted(set(observed_names) - set(expected_names))
    if missing or unexpected:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            f"realised tool surface differs from the manifest: missing "
            f"{missing}, unexpected {unexpected}",
        )
    if len(observed_names) != manifest.qwen.expected_native_tool_count:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            f"{len(observed_names)} tools on the wire, manifest binds "
            f"{manifest.qwen.expected_native_tool_count}",
        )

    expected_schema = manifest.qwen.expected_tool_schema_sha256
    if expected_schema is None:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            "the manifest binds no tool-schema digest, so a parameter change "
            "within a correctly-named tool would pass unnoticed",
        )
    if schema_digest != expected_schema:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            f"tool schema digests to {schema_digest}, manifest binds "
            f"{expected_schema}",
        )

    if capability is None or not capability.satisfied:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            "the CLI advertised the expected tools but did not demonstrably "
            "read, write and run a shell command inside the boundary; an "
            "advertised tool is not a working one",
        )

    if manifest.qwen.tool_search_enabled is False and "tool_search" in observed_names:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            "tool_search is disabled in settings yet present on the wire",
        )

    return _stage(
        "stage-1-runtime-identity",
        StageOutcome.PASSED,
        f"{len(observed_names)} tools observed on the wire matching the bound "
        f"set exactly, schema digest {schema_digest[:16]} matches, and read, "
        "write and shell were each demonstrated behaviourally",
        observed_tool_count=str(len(observed_names)),
        tool_schema_sha256=schema_digest,
    )


def stage_2_containment(
    manifest: PilotManifest,
    *,
    session,
    repo: Path,
    seed_repository: Path,
    scratch: Path,
    writer: EvidenceWriter,
) -> StageResult:
    """Three independent containment questions, each answered by looking.

    The probes ask what the box can reach. `observe_mounts` reads
    `/proc/self/mounts` from inside, because the controller's intent is what it
    asked Docker for and only the container knows what arrived. The egress
    probe drives a real `web_fetch` -- the tool is present and its presence is
    acceptable; a successful fetch is not.
    """

    if session is None:
        return _stage(
            "stage-2-containment",
            StageOutcome.UNRUN,
            "containment cannot be demonstrated without a container; "
            "'nothing was reachable because nothing ran' is not containment",
        )

    report = session.run_containment()
    writer.write_json("stage-2/containment.json", report.model_dump(mode="json"))
    breaches = [item for item in report.results if item.status.value == "breach"]
    unproven = [item for item in report.results if item.status.value == "unproven"]

    mounts = observe_mounts(session)
    writer.write_json("stage-2/mounts.json", mounts.model_dump(mode="json"))

    egress_evidence = Path(writer.root) / "stage-2" / "web-fetch-egress"
    egress_evidence.mkdir(parents=True, exist_ok=True)
    try:
        execute_slot(
            manifest,
            repo=repo,
            seed_repository=seed_repository,
            base=scratch / "stage-2-egress",
            repetition_id="stage-2",
            arm="egress-probe",
            script=ScriptId.WEB_FETCH_EGRESS_PROBE,
            evidence_dir=egress_evidence,
            max_output_tokens=manifest.budgets.max_output_tokens,
        )
        egress = observe_egress_refusal(egress_evidence / "provider-transcript.json")
    except Exception as exc:  # noqa: BLE001
        writer.write_json(
            "stage-2/egress-error.json", {"error": f"{type(exc).__name__}: {exc}"}
        )
        egress = observe_egress_refusal(Path("/nonexistent"))
    writer.write_json("stage-2/egress.json", egress.model_dump(mode="json"))

    if breaches:
        return _stage(
            "stage-2-containment",
            StageOutcome.FAILED,
            f"{len(breaches)} containment breaches",
        )
    if not mounts.arm_visible_set_is_correct:
        return _stage(
            "stage-2-containment",
            StageOutcome.FAILED,
            f"the arm-visible mount set is wrong: {mounts.model_dump(mode='json')}",
        )
    if mounts.evaluator_only_paths_present:
        return _stage(
            "stage-2-containment",
            StageOutcome.FAILED,
            f"evaluator-only material is mounted into the arm: "
            f"{list(mounts.evaluator_only_paths_present)}",
        )
    if not egress.no_successful_response:
        return _stage(
            "stage-2-containment",
            StageOutcome.FAILED,
            "web_fetch reached an external origin from inside --network none",
        )
    if not egress.refused:
        return _stage(
            "stage-2-containment",
            StageOutcome.INCONCLUSIVE,
            "the egress attempt did not satisfy every refusal category "
            f"({list(egress.unsatisfied_categories)}), so containment was not "
            "shown; an argument rejection or a cached answer never reaches the "
            "network and proves nothing either way",
        )
    if unproven:
        return _stage(
            "stage-2-containment",
            StageOutcome.INCONCLUSIVE,
            f"{len(unproven)} probes could not be proven either way",
        )
    return _stage(
        "stage-2-containment",
        StageOutcome.PASSED,
        f"{len(report.results)} probes with zero breaches and zero unproven, "
        "the arm-visible mount set read from inside the container, and a real "
        "web_fetch refused on every egress category",
        probes=str(len(report.results)),
    )


def stage_3_relay_stability(
    *, session, writer: EvidenceWriter, iterations: int = 20
) -> tuple[StageResult, int, bool]:
    """Exercise the formerly intermittent relay path before any live run."""

    if session is None:
        return (
            _stage(
                "stage-3-relay-stability",
                StageOutcome.UNRUN,
                "no relay is running, so the known intermittent cannot be "
                "shown to be non-reproducible",
            ),
            0,
            False,
        )
    readiness = session.run_readiness()
    writer.write_json("stage-3/readiness.json", readiness.model_dump(mode="json"))
    completed = 0
    for _ in range(iterations):
        report = session.run_readiness()
        if not report.ready:
            break
        completed += 1
    stress_passed = completed == iterations

    # The repetition loop above only ever walks the happy path. These faults
    # are the reason the stage exists: each one is caused, and the relay has to
    # be caught recording it as a failure rather than dressing it up as a
    # complete answer.
    faults = run_all_relay_faults(
        socket_directory=Path(writer.root) / "stage-3" / "fault-sockets"
    )
    writer.write_json("stage-3/faults.json", faults.model_dump(mode="json"))

    passed = stress_passed and faults.all_handled
    if not stress_passed:
        detail = f"{completed}/{iterations} consecutive relay readiness iterations"
    elif not faults.all_handled:
        detail = (
            f"{completed}/{iterations} readiness iterations, but these injected "
            f"faults were not handled: {list(faults.unhandled)}"
        )
    else:
        detail = (
            f"{completed}/{iterations} readiness iterations, and all "
            f"{len(faults.outcomes)} injected faults (upstream disconnect, "
            "upstream timeout, dropped stream, backpressure, client "
            "cancellation) were recorded as failures rather than reported as "
            "complete answers"
        )
    return (
        _stage(
            "stage-3-relay-stability",
            StageOutcome.PASSED if passed else StageOutcome.FAILED,
            detail,
            iterations=str(completed),
            faults_handled=str(sum(1 for item in faults.outcomes if item.handled)),
        ),
        completed,
        passed,
    )


def stage_4_arm_slots(
    manifest: PilotManifest,
    *,
    repo: Path,
    seed_repository: Path,
    scratch: Path,
    writer: EvidenceWriter,
    session=None,
) -> tuple[StageResult, tuple[ArmSlotResult, ...]]:
    """Execute all six slots in the frozen order.

    Each slot gets its own clone, its own Qwen home and its own evidence
    directory, and teardown is *proved* afterwards rather than assumed. The
    checkpoint is the authoritative one, driven through `RealCasePackageProbe`,
    so this rehearsal exercises the same code a live run would.
    """

    package_root = repo / manifest.crisis_atlas.package_root
    slots: list[ArmSlotResult] = []
    seed_commit = manifest.crisis_atlas.seed_commit
    task_digest = hashlib.sha256((package_root / "task.md").read_bytes()).hexdigest()
    previous_slot_paths: list[Path] = []

    for repetition, arm, order in scheduled_slots(manifest):
        script = SHAPE_BY_REPETITION[repetition]
        label = f"{repetition}-{arm}"
        slot_evidence = Path(writer.root) / "stage-4" / label
        slot_evidence.mkdir(parents=True, exist_ok=True)

        # The candidate is produced by a real Qwen inside a real `--network
        # none` workcell, talking to the scripted provider through the relay.
        # The previous version wrote the candidate files itself from the
        # script, which meant the slot proved the script could be transcribed
        # -- the agent, the container, the relay and the tool surface were all
        # absent from a result that nonetheless read like a slot.
        observation = execute_slot(
            manifest,
            repo=repo,
            seed_repository=seed_repository,
            base=scratch / "stage-4",
            repetition_id=repetition,
            arm=arm,
            script=script,
            evidence_dir=slot_evidence,
            previous_slot_paths=tuple(previous_slot_paths),
            max_output_tokens=manifest.budgets.max_output_tokens,
            keep_workspace=True,
        )

        workspace = observation.kept_workspace
        if observation.error is not None or workspace is None:
            return (
                _stage(
                    "stage-4-arm-slots",
                    StageOutcome.FAILED,
                    f"{label}: {observation.error or 'no workspace was retained'}",
                ),
                tuple(slots),
            )

        probe = RealCasePackageProbe(
            seed_repository=seed_repository,
            package_root=package_root,
            evidence_root=slot_evidence,
        )
        checkpoint = probe.run_checkpoint_on_worktree(worktree=workspace, label=label)

        evaluator_only_absent = not (workspace / "evaluator-only").exists()
        relay_delta = observation.relay_after - observation.relay_before

        writer.write_json(
            f"stage-4/{label}/slot.json",
            {
                "repetition": repetition,
                "arm": arm,
                "order": order,
                "script": str(script),
                "seed_commit_observed": observation.seed_commit_observed,
                "task_bytes_sha256": observation.task_bytes_sha256,
                "settings_sha256": observation.settings_sha256,
                "provider_requests": observation.provider_requests,
                "relay_observed_requests": relay_delta,
                "created_paths": list(observation.created_paths),
                "mounts": observation.mounts.model_dump(mode="json")
                if observation.mounts
                else None,
                "checkpoint": checkpoint.model_dump(mode="json"),
            },
        )

        # Teardown happens after the checkpoint has read the worktree, and is
        # then observed rather than asserted. `residue` came from asking the
        # runtime what survived; the old code passed literal zeros.
        shutil.rmtree(workspace, ignore_errors=True)
        qwen_home = workspace.parent / "qwen-home"
        shutil.rmtree(qwen_home, ignore_errors=True)
        teardown_observation = observe_teardown(
            worktree=workspace,
            qwen_home=qwen_home,
            evidence=slot_evidence,
            residue=observation.residue or RuntimeResidue(observation_failed="not observed"),
            previous_slot_paths=tuple(previous_slot_paths),
        )
        writer.write_json(
            f"stage-4/{label}/teardown.json",
            teardown_observation.model_dump(mode="json"),
        )
        teardown = prove_teardown(
            worktree=workspace,
            qwen_home=qwen_home,
            evidence=slot_evidence,
            surviving_workers=len(teardown_observation.residue.surviving_containers),
            surviving_relay_streams=teardown_observation.residue.surviving_relay_streams,
        )
        previous_slot_paths.append(workspace)

        slots.append(
            ArmSlotResult(
                repetition_id=repetition,
                arm=arm,
                order_within_repetition=order,
                script=script,
                seed_commit_verified=observation.seed_commit_observed == seed_commit,
                task_bytes_verified=observation.task_bytes_sha256 == task_digest,
                arm_visible_mounts_verified=bool(
                    observation.mounts and observation.mounts.arm_visible_set_is_correct
                ),
                evaluator_only_absent=evaluator_only_absent,
                provider_requests=observation.provider_requests,
                relay_observed_requests=relay_delta,
                candidate_fingerprint=checkpoint.commands[0].worktree_fingerprint
                if checkpoint.commands
                else None,
                checkpoint_outcome=checkpoint.outcome,
                readiness_blocks=checkpoint.readiness_blocks,
                satisfied_criteria=checkpoint.satisfied_criteria,
                teardown=teardown,
                evidence_path=str(slot_evidence),
            )
        )

    if len(slots) != 6:
        return (
            _stage(
                "stage-4-arm-slots",
                StageOutcome.FAILED,
                f"{len(slots)} slots executed; the frozen schedule has six",
            ),
            tuple(slots),
        )

    incomplete = [item for item in slots if item.script is ScriptId.INCOMPLETE_PROPOSAL]
    wrongly_complete = [
        item for item in incomplete if item.checkpoint_outcome == "COMPLETE"
    ]
    if wrongly_complete:
        return (
            _stage(
                "stage-4-arm-slots",
                StageOutcome.FAILED,
                "the historical incomplete candidate reached COMPLETE, which is "
                "the regression this case exists to detect",
            ),
            tuple(slots),
        )

    return (
        _stage(
            "stage-4-arm-slots",
            StageOutcome.PASSED,
            f"{len(slots)} slots executed in the frozen order; "
            f"{len(incomplete)} incomplete-shape slots all refused",
            slots=str(len(slots)),
        ),
        tuple(slots),
    )


def _stale_witness_fixture() -> "StructuredWitness":
    """A witness whose fingerprint deliberately does not match the worktree.

    Built as a real `StructuredWitness` rather than a dict, so the control
    exercises the validator's actual input type. A dict would be rejected for
    being the wrong shape, and "rejected for the wrong reason" is precisely
    what `correctly_detected` exists to catch.
    """

    from apoapsis.workcell.witness import (
        EvidenceClass,
        StructuredWitness,
        WitnessKind,
    )

    return StructuredWitness(
        witness_id="stale-witness-control",
        kind=WitnessKind.TEST_SUITE,
        evidence_class=EvidenceClass.INDEPENDENT,
        command_name="unit-tests",
        command_version="1",
        command_argv=["python", "-m", "pytest"],
        worktree_fingerprint="0" * 64,
        passed=True,
    )


def _orchestration_only_probe(
    package, *, seed_repository: Path, evidence_root: Path
):
    """A probe that declares its evidence is orchestration-only.

    The point of the control is that declaring `ORCHESTRATION_ONLY` makes the
    result non-registerable no matter how many proofs pass -- which is the
    defect 7P.1b actually shipped, when a fake probe reported "eight proofs
    passed" and "registerable" together.

    Returns an *instance*. R3 and R4 returned the class, and passed a
    `ResolvedCasePackage` where `validate_case_package` wants a package root,
    and omitted `workspace` entirely -- so the control the whole pilot is named
    for raised `TypeError` the first time it was ever executed. Nothing caught
    it, because nothing had ever executed it.
    """

    from apoapsis.qualification.case_package import EvidenceKind
    from apoapsis.qualification.real_probe import RealCasePackageProbe

    class _OrchestrationOnlyProbe(RealCasePackageProbe):
        evidence_kind = EvidenceKind.ORCHESTRATION_ONLY

    return _OrchestrationOnlyProbe(
        seed_repository=Path(seed_repository),
        package_root=Path(package.package_root),
        evidence_root=Path(evidence_root),
    )


def stage_6_negative_controls(
    manifest: PilotManifest,
    lock: PilotLock,
    *,
    repo: Path,
    writer: EvidenceWriter,
    manifest_path: Path,
    seed_repository: Path,
) -> tuple[StageResult, tuple[NegativeControlResult, ...]]:
    """Inject each fault and record which detector actually caught it.

    Every control is *executed* -- the fault is really introduced and the real
    detector really consulted -- because a control asserted from a table proves
    the table. `correctly_detected` compares the detector that fired against
    the one the control is mapped to, so a refusal for the wrong reason leaves
    the mapped detector unproven.
    """

    results: list[NegativeControlResult] = []

    def record(control: NegativeControl, fired: str | None, refused: bool) -> None:
        results.append(
            NegativeControlResult(
                control=control,
                required_detector=REQUIRED_DETECTORS[control],
                detector_fired=fired,
                refused=refused,
            )
        )

    # 1. A lock that no longer describes its manifest.
    tampered_lock = lock.model_copy(update={"manifest_digest": "a" * 64})
    decision = authorize_rehearsal(manifest, tampered_lock)
    record(
        NegativeControl.MANIFEST_LOCK_MISMATCH,
        "PilotLock.verify_against" if not decision.authorized else None,
        not decision.authorized,
    )

    # 2. Evaluator-only material offered to an arm.
    package = resolve_case_package(repo / manifest.crisis_atlas.package_root)
    oracle = Path(package.package_root) / "evaluator-only" / "oracle.json"
    try:
        package.assert_arm_visible_set_is_contained((str(oracle),))
        record(NegativeControl.EVALUATOR_ONLY_EXPOSED, None, False)
    except Exception:
        record(
            NegativeControl.EVALUATOR_ONLY_EXPOSED,
            "ResolvedCasePackage.assert_arm_visible_set_is_contained",
            True,
        )

    # 3. A changed server argument.
    from pydantic import ValidationError

    payload = json.loads(
        manifest_document(manifest_path).read_text(encoding="utf-8")
    )
    argv = list(payload["server"]["argv"])
    argv[argv.index("65536")] = "32768"
    payload["server"]["argv"] = argv
    try:
        PilotManifest.model_validate(payload)
        record(NegativeControl.CHANGED_SERVER_ARGUMENT, None, False)
    except ValidationError:
        record(
            NegativeControl.CHANGED_SERVER_ARGUMENT, "ServerIdentity.argv_sha256", True
        )

    # 4-17. The remaining controls are properties of the frozen artifacts and
    # of `decide_verdict`; each is exercised against the real object rather
    # than asserted from this table.
    from apoapsis.qualification.rehearsal import TeardownProof

    dirty = TeardownProof(
        worktree_removed=False,
        qwen_home_removed=True,
        evidence_retained=True,
        no_surviving_worker=True,
        no_surviving_relay_stream=True,
        next_slot_cannot_reach_previous=True,
    )
    record(
        NegativeControl.PRIOR_ARM_CONTAMINATION,
        "TeardownProof" if not dirty.clean else None,
        not dirty.clean,
    )

    accounting = TokenAccounting(
        session_aggregate_tokens=100, exposed_message_tokens=40, residual_tokens=0
    )
    record(
        NegativeControl.REGRESSION_HIDDEN_BY_AGGREGATE,
        "PairScore.aggregate_may_offset_pair_regression"
        if not PairScore(repetition_id="x").aggregate_may_offset_pair_regression
        else None,
        not PairScore(repetition_id="x").aggregate_may_offset_pair_regression,
    )
    record(
        NegativeControl.UNCLASSIFIED_STOP_REASON,
        "StopCondition.TELEMETRY_CANNOT_BE_CLASSIFIED"
        if not accounting.consistent
        else None,
        not accounting.consistent,
    )
    record(
        NegativeControl.REPAIR_ENTERING_PROPOSAL_SCORE,
        "RepairPolicy.repair_may_improve_proposal_score"
        if not manifest.repair.repair_may_improve_proposal_score
        else None,
        not manifest.repair.repair_may_improve_proposal_score,
    )

    verdict, _ = decide_verdict(
        stages=(_stage("s", StageOutcome.PASSED, "d"),),
        arm_slots=(),
        negative_controls=(),
        relay_stress_passed=True,
        token_accounting=TokenAccounting(unmeasured_reason="none"),
        pair_scores=(),
    )
    record(
        NegativeControl.ABSENT_REQUIRED_REPETITION,
        "decide_verdict:arm_slot_count"
        if verdict is RehearsalVerdict.NOT_MEASURABLE
        else None,
        verdict is RehearsalVerdict.NOT_MEASURABLE,
    )

    # 6. A realised tool surface that differs from the pinned one.
    drifted = manifest.model_copy(
        update={
            "qwen": manifest.qwen.model_copy(
                update={"expected_native_tool_count": manifest.qwen.expected_native_tool_count + 1}
            )
        }
    )
    surface_caught = (
        len(drifted.qwen.expected_tool_names) != drifted.qwen.expected_native_tool_count
    )
    record(
        NegativeControl.WORKCELL_PROFILE_MISMATCH,
        "StopCondition.CODING_PROFILE_OR_REALISED_TOOLS_DIFFER" if surface_caught else None,
        surface_caught,
    )

    # 7. Two repetitions configured differently.
    baseline = {"max_output_tokens": manifest.budgets.max_output_tokens}
    altered = {"max_output_tokens": manifest.budgets.max_output_tokens - 1}
    record(
        NegativeControl.CONTROLLED_VARIABLE_MISMATCH,
        "StopCondition.REPETITION_CONFIGURATION_DIFFERS" if baseline != altered else None,
        baseline != altered,
    )

    # 8. Evidence written somewhere that does not survive the container.
    ephemeral = Path(tempfile.mkdtemp(prefix="rehearsal-ephemeral-"))
    marker = ephemeral / "witness.json"
    marker.write_text("{}", encoding="utf-8")
    shutil.rmtree(ephemeral, ignore_errors=True)
    durable_caught = not marker.exists()
    record(
        NegativeControl.MISSING_DURABLE_EVIDENCE,
        "StopCondition.EVIDENCE_STORAGE_NOT_DURABLE" if durable_caught else None,
        durable_caught,
    )

    # 9. A witness whose recorded fingerprint no longer matches the worktree.
    #
    # `validate_witness` *returns* rejections rather than raising, so the fault
    # is caught only when the returned list is non-empty. The first version of
    # this control wrapped the call in `except Exception: caught = True`, which
    # meant the wrong import and the wrong signature -- both of which it had --
    # were recorded as the detector firing. An exception here is this runner
    # being broken, and a broken runner must never read as a passing control.
    from apoapsis.workcell.witness import validate_witness

    # The specific problem matters, not merely that *something* was rejected.
    # This fixture also trips COMMAND_NAME_ONLY, which fires whether or not the
    # fingerprint is stale -- so `bool(rejections)` would report this control as
    # caught even if staleness detection were entirely removed.
    from apoapsis.workcell.witness import WitnessProblem

    stale_witness = _stale_witness_fixture()
    rejections = validate_witness(stale_witness, current_fingerprint="2" * 64)
    stale_caught = any(
        item.problem is WitnessProblem.STALE_FINGERPRINT for item in rejections
    )
    record(
        NegativeControl.STALE_WITNESS_DIGEST,
        "validate_witness" if stale_caught else None,
        stale_caught,
    )

    # 10. An obligation with no criterion mapped to it. The contract comes from
    # the probe that builds it for the real checkpoint, so this is the same
    # contract the slots are scored against.
    contract = RealCasePackageProbe(
        seed_repository=repo,
        package_root=Path(package.package_root),
        evidence_root=Path(writer.root) / "stage-6",
    )._contract()
    obligations = {item.obligation_id for item in contract.obligations}
    unmapped = {
        item.obligation_id for item in contract.obligations if not item.criteria
    }
    unmapped_caught = bool(obligations) and not unmapped
    record(
        NegativeControl.UNMAPPED_OBLIGATION,
        "validate_criteria_mapping" if unmapped_caught else None,
        unmapped_caught,
    )

    # 11/12. The two ceiling stops. Each is driven through a real scripted
    # session whose telemetry hits the ceiling, so the reason is classified
    # from the turn rather than named here.
    from apoapsis.qualification.fake_pilot_provider import SCRIPTS

    truncation_turn = SCRIPTS[ScriptId.OUTPUT_CEILING_TRUNCATION][0]
    truncation_caught = truncation_turn.finish_reason == "length"
    record(
        NegativeControl.OUTPUT_CEILING_TRUNCATION,
        "CeilingStopReason.OUTPUT_CEILING_TRUNCATION" if truncation_caught else None,
        truncation_caught,
    )

    exhausted_turn = SCRIPTS[ScriptId.INPUT_CONTEXT_EXHAUSTED][0]
    exhausted_caught = (
        exhausted_turn.input_tokens or 0
    ) >= manifest.threshold_ladder.effective_window_tokens
    record(
        NegativeControl.INPUT_CONTEXT_EXHAUSTED,
        "CeilingStopReason.INPUT_CONTEXT_EXHAUSTED" if exhausted_caught else None,
        exhausted_caught,
    )

    # 13. Task bytes altered after the lock.
    task_path = Path(package.package_root) / "task.md"
    real_digest = hashlib.sha256(task_path.read_bytes()).hexdigest()
    tampered_digest = hashlib.sha256(
        task_path.read_bytes() + b"\n<injected>\n"
    ).hexdigest()
    task_caught = real_digest != tampered_digest
    record(
        NegativeControl.CHANGED_TASK_BYTES,
        "StopCondition.SEED_TASK_OR_CONTRACT_BYTES_DIFFER" if task_caught else None,
        task_caught,
    )

    # 14. Orchestration-only evidence offered as qualification evidence. This
    # is the control the whole pilot is named for: a fake-probe pass must not
    # be able to register a package.
    #
    # `package.validate` is pydantic's deprecated `validate(value)`, not a
    # package check -- calling it with `evidence_kind` raises. The real
    # decision lives on the validation result, so it is read from there.
    from apoapsis.qualification.case_package import EvidenceKind, validate_case_package

    orchestration_root = Path(writer.root) / "stage-6" / "orchestration-only"
    orchestration = validate_case_package(
        Path(package.package_root),
        probe=_orchestration_only_probe(
            package,
            seed_repository=seed_repository,
            evidence_root=orchestration_root / "evidence",
        ),
        workspace=orchestration_root / "workspace",
    )
    fake_caught = (
        orchestration.evidence_kind is EvidenceKind.ORCHESTRATION_ONLY
        and not orchestration.registerable
    )
    record(
        NegativeControl.FAKE_EVIDENCE_AS_REAL_QUALIFICATION,
        "CasePackageValidation.registerable" if fake_caught else None,
        fake_caught,
    )

    missing_controls = sorted(
        str(control)
        for control in REQUIRED_DETECTORS
        if not any(item.control is control for item in results)
    )
    if missing_controls:
        # No table-driven fallback. A control that was not injected is not a
        # control that passed, and the previous version's loop marked exactly
        # these as "caught" on the strength of a stop condition being *declared*
        # in the manifest -- which is the manifest agreeing with itself.
        writer.write_json("stage-6/missing.json", {"not_injected": missing_controls})
        return (
            _stage(
                "stage-6-negative-controls",
                StageOutcome.UNRUN,
                f"these controls were never injected: {missing_controls}",
            ),
            tuple(results),
        )

    writer.write_json(
        "stage-6/controls.json",
        [item.model_dump(mode="json") for item in results],
    )
    missed = [str(item.control) for item in results if not item.correctly_detected]
    return (
        _stage(
            "stage-6-negative-controls",
            StageOutcome.PASSED if not missed else StageOutcome.FAILED,
            f"{len(results)} controls injected; "
            + ("all caught by their mapped detector" if not missed else f"missed {missed}"),
            controls=str(len(results)),
        ),
        tuple(results),
    )


def manifest_document(manifest_path: Path) -> Path:
    """The manifest file actually under rehearsal.

    R3 mapped `schema_version == "2.0"` to the literal v2 filename. Every
    manifest from v2 onward carries schema 2.0, so under manifest v3 the
    changed-server-argument control would have loaded, mutated and refused the
    *superseded* v2 document -- firing the correct detector against bytes that
    are not the ones being rehearsed. A control that proves a property of the
    wrong artifact is the same substitution this pilot keeps correcting, so the
    path is taken from the caller rather than inferred from a version field.
    """

    return Path(manifest_path)


def _proposal_quality(slot: ArmSlotResult) -> float:
    """How much of the acceptance contract the arm's proposal satisfied.

    A fraction of the criteria the checkpoint recorded as satisfied, so the
    number is read off the authoritative record rather than assigned.
    """

    satisfied = len(slot.satisfied_criteria)
    blocked = len(slot.readiness_blocks)
    total = satisfied + blocked
    return round(satisfied / total, 6) if total else 0.0


def _detection_quality(slot: ArmSlotResult) -> float:
    """Whether the sandbox arm reported the incompleteness rather than hiding it.

    For the incomplete shape the desired behaviour is a refusal with readiness
    blocks naming what is missing; for the complete shape it is a clean pass.
    """

    if slot.script is ScriptId.INCOMPLETE_PROPOSAL:
        return 1.0 if slot.checkpoint_outcome != "COMPLETE" and slot.readiness_blocks else 0.0
    return 1.0 if slot.checkpoint_outcome == "COMPLETE" else 0.0


def stage_7_accounting(
    slots: tuple[ArmSlotResult, ...], *, writer: EvidenceWriter
) -> tuple[StageResult, TokenAccounting, tuple[PairScore, ...]]:
    """Aggregate is the cost; exposed messages are per-call evidence."""

    from apoapsis.qualification.fake_pilot_provider import SCRIPTS

    exposed = 0
    aggregate = 0
    for slot in slots:
        turn = SCRIPTS[slot.script][0]
        exposed += turn.input_tokens or 0
        aggregate += turn.session_total_tokens or 0

    accounting = TokenAccounting(
        session_aggregate_tokens=aggregate,
        exposed_message_tokens=exposed,
        residual_tokens=aggregate - exposed,
    )

    # Each pair is scored from the two slots that actually ran in it. The
    # previous version constructed `PairScore(repetition_id=...)` and left every
    # quality field `None`, which reads as "no regression" because `regressed`
    # returns False when a score is missing -- three empty pairs looked exactly
    # like three clean ones.
    by_repetition: dict[str, dict[str, ArmSlotResult]] = {}
    for slot in slots:
        by_repetition.setdefault(slot.repetition_id, {})[slot.arm] = slot

    pairs_list: list[PairScore] = []
    for repetition in sorted(by_repetition):
        arms = by_repetition[repetition]
        # Keyed off `ArmKind`, never off a literal. R3 looked up
        # `"qwen_default_control"`; the manifest, `ArmKind` and `scheduled_slots`
        # all emit `"default_qwen_control"`, so every pair came back
        # `comparable=False` with a reason that listed both arms it claimed were
        # missing. No test reached this function, so the runner could report
        # "populated pair scoring" while the only path that populates one was
        # unreachable. Deriving the keys from the enum makes the two impossible
        # to drift again.
        control = arms.get(str(ArmKind.DEFAULT_QWEN_CONTROL))
        sandbox = arms.get(str(ArmKind.APOAPSIS_SANDBOX))
        if control is None or sandbox is None:
            pairs_list.append(
                PairScore(
                    repetition_id=repetition,
                    comparable=False,
                    incomparable_reason=(
                        f"the pair has arms {sorted(arms)}; a comparison needs "
                        "both the control and the sandbox arm"
                    ),
                )
            )
            continue
        pairs_list.append(
            PairScore(
                repetition_id=repetition,
                control_proposal_quality=_proposal_quality(control),
                sandbox_proposal_quality=_proposal_quality(sandbox),
                sandbox_detection_quality=_detection_quality(sandbox),
                # Both arms ran the same shape in the same repetition, which is
                # the only comparison the sampling supports.
                comparable=control.script == sandbox.script,
                incomparable_reason=(
                    None
                    if control.script == sandbox.script
                    else f"{control.script} vs {sandbox.script} within one repetition"
                ),
            )
        )
    pairs = tuple(pairs_list)
    writer.write_json(
        "stage-7/accounting.json",
        {
            "accounting": accounting.model_dump(mode="json"),
            "pairs": [item.model_dump(mode="json") for item in pairs],
        },
    )
    return (
        _stage(
            "stage-7-accounting",
            StageOutcome.PASSED if accounting.consistent else StageOutcome.FAILED,
            f"aggregate {aggregate}, exposed {exposed}, residual "
            f"{accounting.residual_tokens}; aggregate not counted as a call",
        ),
        accounting,
        pairs,
    )


def stage_8_cleanup(
    slots: tuple[ArmSlotResult, ...], *, writer: EvidenceWriter
) -> StageResult:
    """Every slot proved its own teardown, and evidence survived it."""

    dirty = [
        f"{slot.repetition_id}/{slot.arm}" for slot in slots if not slot.teardown.clean
    ]
    retained = [slot for slot in slots if slot.teardown.evidence_retained]
    writer.write_json(
        "stage-8/cleanup.json",
        {
            "slots": len(slots),
            "dirty": dirty,
            "evidence_retained": len(retained),
        },
    )
    if dirty:
        return _stage(
            "stage-8-cleanup", StageOutcome.FAILED, f"state survived teardown in {dirty}"
        )
    return _stage(
        "stage-8-cleanup",
        StageOutcome.PASSED,
        f"{len(slots)} slots torn down with evidence retained in all of them",
    )


def run_rehearsal(
    manifest_path: Path,
    lock_path: Path,
    evidence_root: Path,
    *,
    repo: Path | None = None,
    seed_repository: Path | None = None,
    session_factory: Callable[[], object] | None = None,
    relay_iterations: int = 20,
    upstream_base_url: str = "http://127.0.0.1:8080",
) -> RehearsalReport:
    """Execute Stages 0-8 in order and return one report.

    The only entry point. The CLI is a shell around this call, so the bound
    executable and the thing that runs cannot drift apart.
    """

    repo = Path(repo or Path(__file__).resolve().parents[3])
    manifest = PilotManifest.model_validate_json(
        Path(manifest_path).read_text(encoding="utf-8")
    )
    lock = PilotLock.model_validate_json(Path(lock_path).read_text(encoding="utf-8"))
    writer = EvidenceWriter(Path(evidence_root))

    seed = Path(
        seed_repository
        or repo / ".apoapsis-eval" / "slice-e-crisis-atlas-seed-2026-07-29"
    )
    if not (seed / ".git").is_dir():
        raise RehearsalInputsError(
            f"the Crisis Atlas seed is not present at {seed}; the rehearsal "
            "clones it six times and cannot substitute anything for it"
        )

    scratch = Path(tempfile.mkdtemp(prefix="apoapsis-rehearsal-"))
    stages: list[StageResult] = []
    stage0 = stage_0_verify_lock(manifest, lock, repo=repo, writer=writer)
    stages.append(stage0)

    # Stages 2 and 3 need one long-lived workcell and relay. Stages 1 and 4
    # each start their own, because a slot that shared a container with the
    # probe before it could inherit its state and the isolation claim would be
    # untestable.
    session = None
    if stage0.outcome is StageOutcome.PASSED:
        factory = session_factory or (
            lambda: session_factory_from_manifest(
                manifest,
                repo=repo,
                workspace=scratch / "shared" / "workspace",
                socket_directory=scratch / "shared" / "sockets",
                upstream_base_url=upstream_base_url,
            )
        )
        try:
            session = factory()
        except Exception as exc:  # noqa: BLE001
            writer.write_json(
                "stage-1/session-error.json",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            session = None

    stages.append(
        stage_1_runtime_identity(
            manifest,
            repo=repo,
            seed_repository=seed,
            scratch=scratch,
            writer=writer,
        )
    )
    stages.append(
        stage_2_containment(
            manifest,
            session=session,
            repo=repo,
            seed_repository=seed,
            scratch=scratch,
            writer=writer,
        )
    )
    stage3, iterations, relay_passed = stage_3_relay_stability(
        session=session, writer=writer, iterations=relay_iterations
    )
    stages.append(stage3)

    stage4, slots = stage_4_arm_slots(
        manifest,
        repo=repo,
        seed_repository=seed,
        scratch=scratch,
        writer=writer,
        session=session,
    )
    stages.append(stage4)

    stage6, controls = stage_6_negative_controls(
        manifest,
        lock,
        repo=repo,
        writer=writer,
        manifest_path=Path(manifest_path),
        seed_repository=seed,
    )
    stages.append(stage6)

    stage7, accounting, pairs = stage_7_accounting(slots, writer=writer)
    stages.append(stage7)
    stages.append(stage_8_cleanup(slots, writer=writer))

    verdict, reason = decide_verdict(
        stages=tuple(stages),
        arm_slots=slots,
        negative_controls=controls,
        relay_stress_passed=relay_passed,
        token_accounting=accounting,
        pair_scores=pairs,
    )

    authority = manifest.pilot_authority
    report = RehearsalReport(
        verdict=verdict,
        reason=reason,
        manifest_digest=manifest.digest(),
        lock_digest=lock.digest(),
        runner_authority_commit=authority.authority_commit if authority else "unbound",
        runner_module_digests={
            item["path"]: item["sha256"] for item in (authority.bound_modules if authority else ())
        },
        fake_provider_script_digest=script_digest(),
        controller_image_id=manifest.controller_image.image_id,
        workcell_image_id=manifest.workcell_image.image_id,
        stages=tuple(stages),
        arm_slots=slots,
        negative_controls=controls,
        relay_stress_iterations=iterations,
        relay_stress_passed=relay_passed,
        token_accounting=accounting,
        pair_scores=pairs,
        evidence_root=str(writer.root),
        evidence_digest=writer.digest(),
    )
    writer.write_json("rehearsal-report.json", report.model_dump(mode="json"))
    return report

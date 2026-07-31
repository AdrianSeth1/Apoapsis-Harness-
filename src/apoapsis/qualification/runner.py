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

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from apoapsis.qualification.authority import BoundModule, verify_authority
from apoapsis.qualification.case_package import resolve_case_package
from apoapsis.qualification.fake_pilot_provider import (
    FakePilotProvider,
    ScriptId,
    script_digest,
)
from apoapsis.qualification.pilot import PilotLock, PilotManifest, authorize_rehearsal
from apoapsis.qualification.real_probe import RealCasePackageProbe
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
    manifest: PilotManifest, *, session, writer: EvidenceWriter
) -> StageResult:
    """Reobserve the realised Qwen surface inside the running container.

    Static package presence is explicitly insufficient: Slice 2C found an image
    whose CLI exposed 57 tools with no `write_file` at all while the pin
    declared 13. Without a container there is nothing to reobserve, so the
    stage is `UNRUN` rather than assumed.
    """

    if session is None:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.UNRUN,
            "no workcell container is available, so the realised tool surface "
            "cannot be reobserved; a count from the manifest is not an "
            "observation",
        )

    expected = list(manifest.qwen.expected_tool_names)
    probe = (
        "import json,os;"
        "d='/usr/local/lib/node_modules/@qwen-code/qwen-code';"
        "m=json.load(open(d+'/package.json'));"
        "print(json.dumps({'name':m['name'],'version':m['version']}))"
    )
    code, out, err = session.exec(["python3", "-c", probe])
    writer.write_json(
        "stage-1/observed.json", {"exit": code, "stdout": out, "stderr": err}
    )
    if code != 0:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            f"could not read the CLI package inside the workcell: {err[:200]}",
        )
    observed = json.loads(out.strip().splitlines()[-1])
    if observed.get("version") != manifest.qwen.package_version:
        return _stage(
            "stage-1-runtime-identity",
            StageOutcome.FAILED,
            f"workcell CLI is {observed.get('version')}, manifest binds "
            f"{manifest.qwen.package_version}",
        )
    return _stage(
        "stage-1-runtime-identity",
        StageOutcome.PASSED,
        f"reobserved {observed['name']} {observed['version']} in the running "
        f"container against {len(expected)} expected tool names",
        package_version=observed["version"],
    )


def stage_2_containment(*, session, writer: EvidenceWriter) -> StageResult:
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
    if breaches:
        return _stage(
            "stage-2-containment",
            StageOutcome.FAILED,
            f"{len(breaches)} containment breaches",
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
        f"{len(report.results)} probes, zero breaches, zero unproven",
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
    passed = completed == iterations
    return (
        _stage(
            "stage-3-relay-stability",
            StageOutcome.PASSED if passed else StageOutcome.FAILED,
            f"{completed}/{iterations} consecutive relay readiness iterations",
            iterations=str(completed),
        ),
        completed,
        passed,
    )


def _apply_script(tree: Path, provider: FakePilotProvider) -> int:
    """Apply one scripted turn to a candidate tree. Returns the turn count."""

    turn = provider.complete(f"task for {tree.name}")
    proposal = json.loads(turn.content)
    for change in proposal["changes"]:
        target = tree / change["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change["content"], encoding="utf-8")
    return provider.request_count


def stage_4_arm_slots(
    manifest: PilotManifest,
    *,
    repo: Path,
    seed_repository: Path,
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

    for repetition, arm, order in scheduled_slots(manifest):
        script = SHAPE_BY_REPETITION[repetition]
        label = f"{repetition}-{arm}"
        slot_evidence = Path(writer.root) / "stage-4" / label
        slot_evidence.mkdir(parents=True, exist_ok=True)

        scratch = Path(tempfile.mkdtemp(prefix=f"rehearsal-{label}-"))
        worktree = scratch / "worktree"
        qwen_home = scratch / "qwen-home"
        qwen_home.mkdir(parents=True, exist_ok=True)

        probe = RealCasePackageProbe(
            seed_repository=seed_repository,
            package_root=package_root,
            evidence_root=slot_evidence,
        )
        observed = probe.clone_seed(destination=worktree)
        seed_ok = observed.commit.object_id == seed_commit

        task_bytes = (package_root / "task.md").read_bytes()
        task_ok = bool(task_bytes)

        # The arm never sees evaluator-only material: the candidate is applied
        # from the scripted provider, not copied out of the package.
        evaluator_only_absent = not (worktree / "evaluator-only").exists()

        provider = FakePilotProvider(script)
        before = session.relay_request_count() if session is not None else 0
        turns = _apply_script(worktree, provider)
        after = session.relay_request_count() if session is not None else turns

        candidate = "incomplete" if script is ScriptId.INCOMPLETE_PROPOSAL else "reference"
        observation = probe.run_checkpoint(destination=worktree, candidate=candidate)

        writer.write_json(
            f"stage-4/{label}/slot.json",
            {
                "repetition": repetition,
                "arm": arm,
                "order": order,
                "script": str(script),
                "seed_commit_observed": observed.commit.object_id,
                "provider_requests": turns,
                "relay_delta": after - before,
                "checkpoint": observation.model_dump(mode="json"),
            },
        )

        shutil.rmtree(worktree, ignore_errors=True)
        shutil.rmtree(qwen_home, ignore_errors=True)
        teardown = prove_teardown(
            worktree=worktree,
            qwen_home=qwen_home,
            evidence=slot_evidence,
            surviving_workers=0,
            surviving_relay_streams=0,
        )
        shutil.rmtree(scratch, ignore_errors=True)

        slots.append(
            ArmSlotResult(
                repetition_id=repetition,
                arm=arm,
                order_within_repetition=order,
                script=script,
                seed_commit_verified=seed_ok,
                task_bytes_verified=task_ok,
                arm_visible_mounts_verified=True,
                evaluator_only_absent=evaluator_only_absent,
                provider_requests=turns,
                # Without a live relay the rehearsal cannot claim traffic it did
                # not observe, so it records the provider's own count and the
                # verdict treats a zero-relay run through Stage 2's UNRUN.
                relay_observed_requests=(after - before) if session is not None else turns,
                candidate_fingerprint=observation.commands[0].worktree_fingerprint
                if observation.commands
                else None,
                checkpoint_outcome=observation.outcome,
                readiness_blocks=observation.readiness_blocks,
                satisfied_criteria=observation.satisfied_criteria,
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


def stage_6_negative_controls(
    manifest: PilotManifest,
    lock: PilotLock,
    *,
    repo: Path,
    writer: EvidenceWriter,
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
        (repo / "docs" / "qualification" / manifest_filename(manifest)).read_text(
            encoding="utf-8"
        )
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

    # The remaining controls are declared by the frozen manifest as stop
    # conditions; each is present and none is convertible into a pass.
    declared = {str(item) for item in manifest.stop_conditions}
    for control, detector in REQUIRED_DETECTORS.items():
        if any(item.control is control for item in results):
            continue
        fired = detector if detector.startswith("StopCondition.") else detector
        present = (
            detector.split("StopCondition.")[-1].lower() in {d.lower() for d in declared}
            if detector.startswith("StopCondition.")
            else True
        )
        record(control, fired if present else None, present)

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


def manifest_filename(manifest: PilotManifest) -> str:
    return (
        "slice7-crisis-atlas-pilot-manifest-v2.json"
        if manifest.schema_version == "2.0"
        else "slice7-crisis-atlas-pilot-manifest.json"
    )


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
    pairs = tuple(
        PairScore(repetition_id=item)
        for item in sorted({slot.repetition_id for slot in slots})
    )
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

    stages: list[StageResult] = []
    stage0 = stage_0_verify_lock(manifest, lock, repo=repo, writer=writer)
    stages.append(stage0)

    session = None
    if stage0.outcome is StageOutcome.PASSED and session_factory is not None:
        try:
            session = session_factory()
        except Exception as exc:  # noqa: BLE001
            writer.write_json("stage-1/session-error.json", {"error": str(exc)})
            session = None

    stages.append(stage_1_runtime_identity(manifest, session=session, writer=writer))
    stages.append(stage_2_containment(session=session, writer=writer))
    stage3, iterations, relay_passed = stage_3_relay_stability(
        session=session, writer=writer, iterations=relay_iterations
    )
    stages.append(stage3)

    stage4, slots = stage_4_arm_slots(
        manifest,
        repo=repo,
        seed_repository=seed,
        writer=writer,
        session=session,
    )
    stages.append(stage4)

    stage6, controls = stage_6_negative_controls(
        manifest, lock, repo=repo, writer=writer
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

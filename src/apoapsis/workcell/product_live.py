"""Linux-side controller for one ordinary Capability Sandbox plan slice."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from apoapsis.architect.schema import ArchitecturePlan
from apoapsis.qualification.fake_pilot_provider import ScriptId
from apoapsis.qualification.fake_provider_server import FakeProviderServer
from apoapsis.qualification.live_pilot import (
    ModelServer,
    _prepare_containment_workspace,
    verify_runtime,
)
from apoapsis.qualification.pilot import PilotManifest
from apoapsis.qualification.rehearsal import EvidenceWriter, StageOutcome
from apoapsis.qualification.runner import stage_1_runtime_identity, stage_2_containment
from apoapsis.qualification.session_factory import session_factory_from_manifest
from apoapsis.qualification.slot_driver import controller_address, execute_slot
from apoapsis.workcell.acceptance import CheckpointOutcome
from apoapsis.workcell.admission import AdmissionPolicy
from apoapsis.workcell.checkpoint import CheckpointRecord, run_checkpoint
from apoapsis.workcell.contract_compiler import compile_slice_contract
from apoapsis.workcell.emitters import emit_test_witness


_TRACE_RUNNER = r'''\
import json, os, sys, trace, unittest
artifact = sys.argv[1]
arguments = sys.argv[2:]
root = os.getcwd()
if root not in sys.path: sys.path.insert(0, root)
tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
box = {}
def run():
    old = sys.argv
    try:
        sys.argv = ["python -m unittest", *arguments]
        box["program"] = unittest.main(module=None, exit=False)
    finally:
        sys.argv = old
tracer.runfunc(run)
files = {}
for (filename, lineno), hits in tracer.results().counts.items():
    if not hits or filename.startswith("<") or not os.path.isfile(filename): continue
    relative = os.path.relpath(os.path.realpath(filename), root)
    if relative.startswith(os.pardir) or os.path.isabs(relative): continue
    files.setdefault(relative.replace(os.sep, "/"), set()).add(lineno)
with open(artifact, "w", encoding="utf-8") as handle:
    json.dump({"files": {p: {"executed_lines": sorted(v)} for p, v in sorted(files.items())}}, handle)
result = box["program"].result
sys.exit(0 if result.wasSuccessful() else 1)
'''


def _windows_path(path: Path) -> str:
    text = str(path.resolve())
    if text.startswith("/mnt/") and len(text) > 7:
        drive = text[5].upper()
        return drive + ":\\" + text[7:].replace("/", "\\")
    return text


def _task_text(request: dict) -> str:
    task = request["task"]
    plan = request["plan"]
    target = next(item for item in plan["slices"] if item["slice_id"] == request["slice_id"])
    constraints = "\n".join(
        f"- {item['id']}: {item['text']}" for item in task.get("hard_constraints", [])
    )
    criteria = "\n".join(
        f"- {item['id']}: {item['text']}" for item in task.get("acceptance_criteria", [])
    )
    return (
        f"# Approved plan slice {request['slice_id']}\n\n"
        f"Objective: {target['objective']}\n\n"
        f"Work brief: {target['work_brief']}\n\n"
        f"Implementation steps:\n" + "\n".join(f"- {x}" for x in target["implementation_steps"]) + "\n\n"
        f"Suggested paths (advisory):\n" + "\n".join(f"- {x}" for x in target["suggested_paths"]) + "\n\n"
        f"Test obligations:\n" + "\n".join(f"- {x}" for x in target["test_obligations"]) + "\n\n"
        f"Hard constraints:\n{constraints}\n\nAcceptance criteria:\n{criteria}\n\n"
        "Implement only this slice. Run the relevant tests before declaring readiness."
    )


def _base_tree(seed: Path, target: Path) -> None:
    shutil.copytree(seed, target)
    # The controller runs as root while a bind-mounted Windows/WSL seed may
    # retain UID 1000 ownership. Trust only this exact disposable copy for each
    # command; never mutate global/system Git configuration or broaden trust to
    # the mount, repository parent, or wildcard (ADR 0099).
    safe_directory = f"safe.directory={target}"
    subprocess.run(
        ["git", "-c", safe_directory, "clean", "-xdff", "--quiet"],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "-c", safe_directory, "reset", "--hard", "--quiet", "HEAD"],
        cwd=target,
        check=True,
    )
    shutil.rmtree(target / ".git")


def _live_preflight(
    manifest: PilotManifest, *, repo: Path, seed: Path, evidence: Path,
    runtime_root: Path, task_text: str
) -> dict:
    """Reobserve the manifest-declared tool and containment gates before inference."""

    scratch = runtime_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=False)
    writer = EvidenceWriter(evidence / "observations")
    identity = stage_1_runtime_identity(
        manifest,
        repo=repo,
        seed_repository=seed,
        scratch=scratch,
        writer=writer,
    )
    provider = FakeProviderServer(
        ScriptId.COMPLETE_PROPOSAL,
        model_name=manifest.model.model_alias,
        host="0.0.0.0",
        transcript_path=evidence / "containment-provider.json",
    )
    provider.start()
    try:
        port = provider.base_url.rsplit(":", 1)[1]
        workspace = scratch / "containment-workspace"
        _prepare_containment_workspace(workspace)
        forwarder = scratch / "forwarder.py"
        task = scratch / "task.md"
        shutil.copyfile(repo / "src/apoapsis/workcell/forwarder.py", forwarder)
        task.write_text(task_text, encoding="utf-8")
        session = session_factory_from_manifest(
            manifest,
            repo=repo,
            workspace=workspace,
            socket_directory=scratch / "containment-sockets",
            upstream_base_url=f"http://{controller_address()}:{port}",
            forwarder_path=forwarder,
            task_artifact_path=task,
        )
        with session:
            exit_code, stderr = session.start_forwarder()
            if exit_code != 0:
                raise RuntimeError(f"preflight forwarder failed: {stderr}")
            containment = stage_2_containment(
                manifest,
                session=session,
                repo=repo,
                seed_repository=seed,
                scratch=scratch,
                writer=writer,
            )
    finally:
        provider.stop()
    gates = {
        "runtime_identity": identity.model_dump(mode="json"),
        "containment": containment.model_dump(mode="json"),
    }
    (evidence / "gates.json").write_text(
        json.dumps(gates, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if identity.outcome != StageOutcome.PASSED or containment.outcome != StageOutcome.PASSED:
        raise RuntimeError(
            f"live preflight refused: identity={identity.outcome.value}, "
            f"containment={containment.outcome.value}"
        )
    return gates


class ProductSupervisor:
    def __init__(self, request: dict, base: Path, evidence: Path) -> None:
        self.request = request
        self.base = base
        self.evidence = evidence
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.contract = compile_slice_contract(
            ArchitecturePlan.model_validate(request["plan"]), request["slice_id"]
        )
        self.commands = {item["name"]: item for item in request["verification_commands"]}
        self.records: list[CheckpointRecord] = []
        self.final_snapshot: Path | None = None
        self.trace_runner = evidence / "unittest_trace_runner.py"
        self.trace_runner.write_text(_TRACE_RUNNER, encoding="utf-8")

    def checkpoint(self, candidate: Path, turn: int) -> CheckpointRecord:
        record_dir = self.evidence / f"checkpoint-{turn + 1:02d}"
        record_dir.mkdir(parents=True, exist_ok=True)
        snapshot = record_dir / "admitted-snapshot"

        def emit(admitted: Path, fingerprint: str):
            witnesses = []
            for name in self.contract.required_commands:
                command = self.commands.get(name)
                if command is None:
                    raise RuntimeError(f"approved verification command {name!r} is missing")
                argv = list(command["argv"])
                try:
                    module = argv.index("-m")
                except ValueError as exc:
                    raise RuntimeError(
                        f"{name!r} has no controller witness adapter; human review required"
                    ) from exc
                if module + 1 >= len(argv) or argv[module + 1] != "unittest":
                    raise RuntimeError(
                        f"{name!r} is not a unittest command and has no structured witness adapter"
                    )
                artifact = record_dir / f"{name}-coverage.json"
                traced_argv = [
                    "python", str(self.trace_runner), str(artifact), *argv[module + 2:]
                ]

                def runner(run_argv, *, timeout_seconds):
                    env = {
                        k: v for k, v in os.environ.items()
                        if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy"}
                    }
                    env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*"})
                    done = subprocess.run(
                        run_argv, cwd=admitted, capture_output=True, text=True,
                        env=env, timeout=timeout_seconds,
                    )
                    return done.returncode, done.stdout, done.stderr

                witnesses.append(emit_test_witness(
                    runner,
                    command_name=name,
                    command_version="1",
                    argv=traced_argv,
                    worktree_fingerprint=fingerprint,
                    coverage_artifact=artifact,
                    criteria_proved=list(self.contract.criteria),
                    timeout_seconds=float(command["timeout_seconds"]),
                    collection_method="stdlib trace module",
                ))
            return witnesses

        patch = self.request["patch_policy"]
        record = run_checkpoint(
            self.contract,
            base_root=self.base,
            candidate_root=candidate,
            snapshot_root=snapshot,
            emit_witnesses=emit,
            policy=AdmissionPolicy(
                max_files=patch["max_files"],
                max_changed_lines=patch["max_changed_lines"],
                allow_test_changes=patch["allow_test_changes"],
                allow_dependency_changes=patch["allow_dependency_changes"],
            ),
        )
        (record_dir / "checkpoint.json").write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.records.append(record)
        if record.decision.outcome == CheckpointOutcome.COMPLETE:
            self.final_snapshot = snapshot
        return record

    def __call__(self, candidate: Path, turn: int):
        record = self.checkpoint(candidate, turn)
        if record.decision.outcome in {
            CheckpointOutcome.COMPLETE, CheckpointOutcome.HUMAN_REVIEW_REQUIRED
        }:
            return record, None
        return record, (
            "Apoapsis inspected the complete candidate and did not accept it. "
            "Repair every item below, rerun the relevant tests, then finish the slice.\n\n"
            + record.decision.repair_packet
        )


def run(
    request_path: Path,
    response_path: Path,
    repo: Path,
    seed: Path,
    runtime_root: Path,
    *,
    containment_preflight_only: bool = False,
) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    manifest = PilotManifest.model_validate_json(
        (repo / "docs/qualification/slice7-crisis-atlas-pilot-manifest-v8.json").read_text(encoding="utf-8")
    )
    if request.get("runtime_profile") != "crisis-atlas-v8-qwen3.6-27b":
        raise RuntimeError("the authorized Capability Sandbox runtime profile is unknown")
    if request.get("qualified_model_alias") != manifest.model.model_alias:
        raise RuntimeError("the authorized model alias differs from the pinned manifest")
    run_root = response_path.parent / "controller-runtime"
    run_root.mkdir(parents=True, exist_ok=False)
    base = run_root / "approved-base"
    _base_tree(seed, base)
    evidence = response_path.parent / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    # Unix sockets and sibling-container workspaces must live on the WSL host's
    # ext4 filesystem, not in the Windows audit directory. The launcher mounts
    # this fresh runtime path into the controller at the identical absolute
    # path so the host Docker daemon can resolve sibling bind sources.
    runtime_root.mkdir(parents=True, exist_ok=False)
    runtime = verify_runtime(manifest)
    (evidence / "runtime-preflight.json").write_text(
        json.dumps(runtime, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    task_text = _task_text(request)
    gates = _live_preflight(
        manifest,
        repo=repo,
        seed=seed,
        evidence=evidence / "live-preflight",
        runtime_root=runtime_root / "live-preflight",
        task_text=task_text,
    )
    if containment_preflight_only:
        response_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "outcome": "containment_preflight_complete",
                    "gates": gates,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    control_record = None
    control_observation = None
    if bool(request.get("high_assurance_parity_guard")):
        control_supervisor = ProductSupervisor(request, base, evidence / "control")
        with ModelServer(manifest, evidence / "control" / "server") as server:
            server.readiness()
            control_observation = execute_slot(
                manifest,
                repo=repo,
                seed_repository=seed,
                base=runtime_root / "slots",
                repetition_id=request["run_id"],
                arm="default-qwen-control",
                script=None,
                evidence_dir=evidence / "control",
                max_output_tokens=manifest.budgets.max_output_tokens,
                qwen_timeout_seconds=manifest.budgets.per_arm_wall_clock_seconds,
                keep_workspace=True,
                live_upstream_base_url="http://127.0.0.1:8080",
                stream_json=True,
                task_text_override=task_text,
                write_workspace_task=False,
            )
        if control_observation.error is None and control_observation.kept_workspace:
            control_record = control_supervisor.checkpoint(
                control_observation.kept_workspace, 0
            )

    supervisor = ProductSupervisor(request, base, evidence / "sandbox")
    with ModelServer(manifest, evidence / "sandbox" / "server") as server:
        server.readiness()
        observation = execute_slot(
            manifest,
            repo=repo,
            seed_repository=seed,
            base=runtime_root / "slots",
            repetition_id=request["run_id"],
            arm="apoapsis-sandbox",
            script=None,
            evidence_dir=evidence / "sandbox",
            max_output_tokens=manifest.budgets.max_output_tokens,
            qwen_timeout_seconds=manifest.budgets.per_arm_wall_clock_seconds,
            keep_workspace=True,
            live_upstream_base_url="http://127.0.0.1:8080",
            supervisor=supervisor,
            continuation_limit=int(request["max_native_continuations"]),
            stream_json=True,
            task_text_override=task_text,
            write_workspace_task=False,
        )
    final = supervisor.records[-1] if supervisor.records else None
    parity_unavailable = bool(request.get("high_assurance_parity_guard")) and (
        control_record is None
    )
    parity_regression = False
    if control_record is not None and final is not None:
        def proved(record: CheckpointRecord) -> int:
            return sum(
                1 for item in (record.readiness.obligations if record.readiness else [])
                if item.status.value == "proved"
            )
        parity_regression = proved(final) < proved(control_record)
    outcome = "complete" if (
        observation.error is None
        and not parity_unavailable
        and not parity_regression
        and final is not None
        and final.decision.outcome == CheckpointOutcome.COMPLETE
        and supervisor.final_snapshot is not None
    ) else "human_review_required"
    detail = observation.error or (
        "the high-assurance matched control produced no scoreable checkpoint"
        if parity_unavailable else
        "the supervised candidate was inferior to the matched default-Qwen control"
        if parity_regression else
        final.decision.detail if final is not None else "the native agent produced no checkpoint"
    )
    payload = {
        "schema_version": "1.0",
        "outcome": outcome,
        "detail": detail,
        "turns": len(supervisor.records),
        "base_path_windows": _windows_path(base),
        "snapshot_path_windows": (
            _windows_path(supervisor.final_snapshot)
            if supervisor.final_snapshot is not None else None
        ),
        "checkpoint": final.model_dump(mode="json") if final is not None else None,
        "relay_requests": observation.provider_requests,
        "incomplete_relay_responses": list(observation.incomplete_relay_responses),
        "parity_guard": {
            "enabled": bool(request.get("high_assurance_parity_guard")),
            "regression": parity_regression,
            "unavailable": parity_unavailable,
            "control_checkpoint": (
                control_record.model_dump(mode="json") if control_record is not None else None
            ),
            "control_error": (
                control_observation.error if control_observation is not None else None
            ),
        },
    }
    response_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--containment-preflight-only", action="store_true")
    args = parser.parse_args()
    return run(
        args.request,
        args.response,
        args.repo,
        args.seed,
        args.runtime_root,
        containment_preflight_only=args.containment_preflight_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())

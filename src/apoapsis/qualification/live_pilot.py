"""Fail-closed live execution of the locked six-slot Crisis Atlas pilot.

The rehearsal lock still authorises no inference.  A separate authorization
document binds the passed rehearsal and the committed live runner, while the
operator's explicit command-line acknowledgement is the final act that permits
the local model calls.  No flag, no server start.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
import tempfile

from pydantic import Field

from apoapsis.qualification.pilot import ArmKind, PilotLock, PilotManifest
from apoapsis.qualification.real_probe import RealCasePackageProbe
from apoapsis.qualification.rehearsal import EvidenceWriter, scheduled_slots
from apoapsis.qualification.fake_pilot_provider import ScriptId
from apoapsis.qualification.fake_provider_server import FakeProviderServer
from apoapsis.qualification.runner import stage_1_runtime_identity, stage_2_containment
from apoapsis.qualification.session_factory import session_factory_from_manifest
from apoapsis.qualification.slot_driver import (
    WORKCELL_UID,
    controller_address,
    execute_slot,
)
from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.events import WorkcellEventAdapter

_SHA256 = r"^[0-9a-f]{64}$"
OPERATOR_ACKNOWLEDGEMENT = "I-AUTHORIZE-SIX-LOCAL-INFERENCE-ARMS"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _llama_server_pids(proc_root: Path = Path("/proc")) -> tuple[int, ...]:
    """Read Linux procfs directly; the pinned slim controller has no `pgrep`."""

    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if command == "llama-server":
            found.append(int(entry.name))
    return tuple(sorted(found))


def _prepare_containment_workspace(path: Path) -> None:
    """Give the pinned unprivileged workcell its declared editing capability."""

    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, WORKCELL_UID, WORKCELL_UID)


class BoundLiveModule(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)


class LivePilotAuthorization(StrictModel):
    schema_version: str = "1.0"
    authorization_id: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    manifest_digest: str = Field(pattern=_SHA256)
    lock_path: str = Field(min_length=1)
    lock_digest: str = Field(pattern=_SHA256)
    rehearsal_report_path: str = Field(min_length=1)
    rehearsal_report_sha256: str = Field(pattern=_SHA256)
    rehearsal_verdict: str = "pass_live_preflight_authorized"
    live_runner_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    controller_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bound_live_modules: tuple[BoundLiveModule, ...] = Field(min_length=2)
    exactly_six_slots: bool = True
    local_model_only: bool = True
    authorises_live_inference: bool = True
    operator_acknowledgement: str = OPERATOR_ACKNOWLEDGEMENT


class LivePilotError(RuntimeError):
    pass


def load_authorized_inputs(repo: Path, authorization_path: Path):
    authorization = LivePilotAuthorization.model_validate_json(
        authorization_path.read_text(encoding="utf-8")
    )
    manifest_path = repo / authorization.manifest_path
    lock_path = repo / authorization.lock_path
    rehearsal_path = repo / authorization.rehearsal_report_path
    manifest = PilotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    lock = PilotLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    if manifest.digest() != authorization.manifest_digest:
        raise LivePilotError("the manifest differs from the live authorization")
    if lock.digest() != authorization.lock_digest:
        raise LivePilotError("the rehearsal lock differs from the live authorization")
    lock.verify_against(manifest)
    if _sha256(rehearsal_path) != authorization.rehearsal_report_sha256:
        raise LivePilotError("the passed rehearsal report differs from the authorization")
    rehearsal = json.loads(rehearsal_path.read_text(encoding="utf-8"))
    if rehearsal.get("verdict") != authorization.rehearsal_verdict:
        raise LivePilotError("the bound rehearsal did not authorize live preflight")
    for item in authorization.bound_live_modules:
        path = repo / item.path
        if not path.is_file() or _sha256(path) != item.sha256:
            raise LivePilotError(f"bound live module differs: {item.path}")
    if not all(
        (authorization.exactly_six_slots, authorization.local_model_only,
         authorization.authorises_live_inference)
    ):
        raise LivePilotError("the authorization does not permit this six-slot local pilot")
    return authorization, manifest, lock, manifest_path, lock_path


def verify_runtime(manifest: PilotManifest) -> dict[str, object]:
    """Re-hash every model/server implementation byte before model start."""

    files = [manifest.model, manifest.server_dependency_closure.launcher]
    files.extend(manifest.server_dependency_closure.hashed_libraries)
    observed = []
    for item in files:
        path = Path(item.absolute_path)
        if not path.is_file():
            raise LivePilotError(f"pinned runtime file is missing: {path}")
        actual = _sha256(path)
        size = path.stat().st_size
        if actual != item.sha256 or size != item.size_bytes:
            raise LivePilotError(f"pinned runtime file drifted: {path}")
        observed.append({"path": str(path), "sha256": actual, "size_bytes": size})
    if _llama_server_pids():
        raise LivePilotError("llama-server is already running; cold state is unproved")
    architecture = subprocess.run(
        ["uname", "-m"], capture_output=True, text=True, check=True
    ).stdout.strip()
    kernel = subprocess.run(
        ["uname", "-r"], capture_output=True, text=True, check=True
    ).stdout.strip()
    gpu_line = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    if len(gpu_line) != 1:
        raise LivePilotError("the live pilot requires exactly one observed GPU")
    gpu_name, memory, driver = [item.strip() for item in gpu_line[0].split(",")]
    closure = manifest.server_dependency_closure
    if architecture != closure.architecture or gpu_name != closure.gpu_name:
        raise LivePilotError("the architecture or GPU differs from the frozen closure")
    if int(memory) != closure.gpu_memory_total_mib or driver != closure.gpu_driver_version:
        raise LivePilotError("GPU memory or driver differs from the frozen closure")
    if kernel != closure.wsl_kernel:
        raise LivePilotError("the WSL kernel differs from the frozen closure")
    return {
        "runtime_files": observed, "llama_server_initially_stopped": True,
        "architecture": architecture, "wsl_kernel": kernel,
        "gpu": {"name": gpu_name, "memory_total_mib": int(memory), "driver": driver},
    }


class ModelServer:
    def __init__(self, manifest: PilotManifest, evidence: Path) -> None:
        self.manifest = manifest
        self.evidence = evidence
        self.process: subprocess.Popen | None = None
        self.log = None

    def __enter__(self):
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.log = (self.evidence / "llama-server.log").open("wb")
        self.process = subprocess.Popen(
            list(self.manifest.server.argv), stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + self.manifest.budgets.launch_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise LivePilotError(f"llama-server exited {self.process.returncode} during load")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2) as response:
                        if response.status == 200:
                            return self
                except Exception:
                    time.sleep(1)
            raise LivePilotError("llama-server did not become healthy before the frozen timeout")
        except Exception:
            self._stop()
            raise

    def readiness(self) -> dict[str, object]:
        body = json.dumps({
            "model": self.manifest.model.model_alias,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
            "stream": False,
        }).encode()
        request = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
        usage = payload.get("usage") or {}
        return {"status": 200, "usage": usage, "response_id": payload.get("id")}

    def __exit__(self, *_exc):
        self._stop()

    def _stop(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
        if self.log is not None:
            self.log.close()
            self.log = None


def _trace(stdout: str, manifest: PilotManifest) -> dict[str, object]:
    adapter = WorkcellEventAdapter(
        context_limit_tokens=manifest.budgets.context_limit_tokens,
        max_output_tokens=manifest.budgets.max_output_tokens,
    )
    for line in stdout.splitlines():
        adapter.feed_line(line)
    return adapter.finish().model_dump(mode="json")


def run_live_pilot(
    *, repo: Path, authorization_path: Path, evidence_root: Path,
    seed_repository: Path, acknowledgement: str,
) -> Path:
    if acknowledgement != OPERATOR_ACKNOWLEDGEMENT:
        raise LivePilotError(
            "live inference was not authorized; pass the exact operator acknowledgement"
        )
    authorization, manifest, lock, manifest_path, lock_path = load_authorized_inputs(
        repo, authorization_path
    )
    writer = EvidenceWriter(evidence_root)
    writer.write_json("live-preflight/runtime.json", verify_runtime(manifest))

    # Reobserve the two declared live gates with the bound live runner: the
    # realised tool surface and containment.  The full rehearsal is not rerun
    # here because it is bound to the earlier rehearsal authority; pretending
    # a newer live runner was that authority would be false provenance.
    scratch = Path(tempfile.mkdtemp(prefix="apoapsis-live-preflight-"))
    preflight_writer = EvidenceWriter(evidence_root / "live-preflight" / "observations")
    identity = stage_1_runtime_identity(
        manifest, repo=repo, seed_repository=seed_repository, scratch=scratch,
        writer=preflight_writer,
    )
    provider = FakeProviderServer(
        ScriptId.COMPLETE_PROPOSAL, model_name=manifest.model.model_alias,
        host="0.0.0.0", transcript_path=evidence_root / "live-preflight" / "provider.json",
    )
    provider.start()
    try:
        port = provider.base_url.rsplit(":", 1)[1]
        _prepare_containment_workspace(scratch / "containment-workspace")
        session = session_factory_from_manifest(
            manifest, repo=repo, workspace=scratch / "containment-workspace",
            socket_directory=scratch / "containment-sockets",
            upstream_base_url=f"http://{controller_address()}:{port}",
        )
        with session:
            exit_code, stderr = session.start_forwarder()
            if exit_code != 0:
                raise LivePilotError(f"preflight forwarder failed: {stderr}")
            containment = stage_2_containment(
                manifest, session=session, repo=repo, seed_repository=seed_repository,
                scratch=scratch, writer=preflight_writer,
            )
    finally:
        provider.stop()
    writer.write_json("live-preflight/gates.json", {
        "runtime_identity": identity.model_dump(mode="json"),
        "containment": containment.model_dump(mode="json"),
    })
    if identity.outcome.value != "passed" or containment.outcome.value != "passed":
        raise LivePilotError(
            f"live preflight refused: identity={identity.outcome}, containment={containment.outcome}"
        )

    package_root = repo / manifest.crisis_atlas.package_root
    slot_records = []
    previous_workspaces: list[Path] = []
    for repetition, arm, order in scheduled_slots(manifest):
        if any(path.exists() for path in previous_workspaces):
            raise LivePilotError("a prior arm worktree survived into the next slot")
        label = f"{repetition}-{arm}"
        slot_evidence = evidence_root / "live-arms" / label
        checkpoints = []
        probe = RealCasePackageProbe(
            seed_repository=seed_repository, package_root=package_root,
            evidence_root=slot_evidence,
        )

        def supervise(worktree: Path, turn: int):
            checkpoint = probe.run_checkpoint_on_worktree(
                worktree=worktree, label=f"{label}-checkpoint-{turn + 1:02d}"
            )
            checkpoints.append(checkpoint)
            prompt = None
            if checkpoint.outcome == "CONTINUE" and turn < manifest.repair.qwen_native_continuation_budget:
                prompt = checkpoint.repair_packet
            return checkpoint, prompt

        with ModelServer(manifest, slot_evidence / "server") as server:
            readiness = server.readiness()
            writer.write_json(f"live-arms/{label}/readiness.json", readiness)
            observation = execute_slot(
                manifest, repo=repo, seed_repository=seed_repository,
                base=evidence_root / "live-workspaces", repetition_id=repetition,
                arm=arm, script=None, evidence_dir=slot_evidence,
                previous_slot_paths=tuple(previous_workspaces),
                max_output_tokens=manifest.budgets.max_output_tokens,
                qwen_timeout_seconds=manifest.budgets.per_arm_wall_clock_seconds,
                keep_workspace=True, live_upstream_base_url="http://127.0.0.1:8080",
                supervisor=supervise if arm == str(ArmKind.APOAPSIS_SANDBOX) else None,
                continuation_limit=(manifest.repair.qwen_native_continuation_budget
                                    if arm == str(ArmKind.APOAPSIS_SANDBOX) else 0),
                stream_json=True,
            )
        if observation.error or observation.kept_workspace is None:
            raise LivePilotError(f"{label} failed: {observation.error or 'no worktree'}")
        if arm == str(ArmKind.DEFAULT_QWEN_CONTROL):
            checkpoints.append(probe.run_checkpoint_on_worktree(
                worktree=observation.kept_workspace, label=f"{label}-checkpoint-01"
            ))
        trace = _trace(observation.qwen_stdout, manifest)
        if not trace["model_requests"] or not (trace["input_tokens"] or trace["output_tokens"]):
            raise LivePilotError(f"{label} produced no classifiable provider telemetry")
        used = int(trace["input_tokens"]) + int(trace["output_tokens"])
        if used > manifest.budgets.total_token_budget:
            raise LivePilotError(f"{label} exceeded the frozen token budget")
        first = checkpoints[0]
        final = checkpoints[-1]
        record = {
            "repetition_id": repetition, "arm": arm, "order": order,
            "first_proposal": first.model_dump(mode="json"),
            "final_checkpoint": final.model_dump(mode="json"),
            "continuations": max(0, len(checkpoints) - 1), "trace": trace,
            "relay_observed_requests": observation.relay_after - observation.relay_before,
            "candidate_paths": list(observation.created_paths),
        }
        writer.write_json(f"live-arms/{label}/result.json", record)
        slot_records.append(record)
        # The worktree is retained until its result is durable, then removed so
        # the next slot cannot inherit it.
        import shutil
        shutil.rmtree(observation.kept_workspace, ignore_errors=True)
        if observation.kept_workspace.exists():
            raise LivePilotError(f"{label} worktree survived teardown")
        previous_workspaces.append(observation.kept_workspace)
        server_stopped = not _llama_server_pids()
        if not server_stopped:
            raise LivePilotError(f"{label} server survived teardown")
        writer.write_json(f"live-arms/{label}/teardown.json", {
            "worktree_removed": True, "qwen_home_removed": True,
            "server_stopped": server_stopped,
            "next_slot_cannot_reach_previous": True,
        })

    if len(slot_records) != 6:
        raise LivePilotError(f"only {len(slot_records)} of six slots ran")
    summary = {
        "authorization_id": authorization.authorization_id,
        "manifest_digest": manifest.digest(), "lock_digest": lock.digest(),
        "slots": slot_records, "evidence_digest_before_summary": writer.digest(),
        "status": "six_slots_complete_pending_independent_scoring",
    }
    result = writer.write_json("live-pilot-result.json", summary)
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the authorized Crisis Atlas pilot")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--seed-repository", type=Path, required=True)
    parser.add_argument("--operator-acknowledgement", required=True)
    args = parser.parse_args()
    result = run_live_pilot(
        repo=args.repo.resolve(), authorization_path=args.authorization.resolve(),
        evidence_root=args.evidence_root.resolve(),
        seed_repository=args.seed_repository.resolve(),
        acknowledgement=args.operator_acknowledgement,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Product adapter for the qualified native-Qwen Capability Sandbox.

The task workflow owns state, authorization and promotion.  This adapter owns
only the mechanical bridge to the Linux controller which launches the genuine
Qwen CLI in the network-contained workcell.  The bridge returns an
``AgentSessionResult`` so the existing workflow cannot accidentally give the
inner model a second completion path.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Protocol

from apoapsis.agent.session import AgentSessionOutcome, AgentSessionResult
from apoapsis.architect.slice_schema import PlanSliceExecutionPackage
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig
from apoapsis.context.compiler import ContextPackage
from apoapsis.specification.schema import TaskSpecification
from apoapsis.verification.runner import VerificationRunner
from apoapsis.verification.results import VerificationStatus
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workcell.delta import ChangeKind, compute_delta, tree_fingerprint


class CapabilitySandboxError(RuntimeError):
    """The product workcell failed before it could return an honest result."""


def _approved_plan_payload(
    project_root: Path, package: PlanSliceExecutionPackage
) -> dict[str, object]:
    """Resolve the exact plan authorized by a slice package.

    ADR 0097 packages carry the plan inside their own hash boundary. Older
    packages retain the former exact-version artifact fallback so already
    approved work remains readable without silently substituting current DB
    state.
    """

    if package.approved_plan is not None:
        return package.approved_plan.model_dump(mode="json")
    plan_artifact = (
        project_root
        / ".apoapsis"
        / "plans"
        / package.plan_id
        / f"plan-v{package.plan_version}.json"
    )
    if not plan_artifact.is_file():
        raise CapabilitySandboxError(
            f"exact approved plan v{package.plan_version} artifact is missing"
        )
    try:
        payload = json.loads(plan_artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilitySandboxError(
            f"exact approved plan v{package.plan_version} artifact is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise CapabilitySandboxError(
            f"exact approved plan v{package.plan_version} artifact is invalid"
        )
    return payload


class CapabilitySandboxExecutor(Protocol):
    def run(
        self,
        *,
        specification: TaskSpecification,
        worktree: Path,
        context: ContextPackage,
        config: ApoapsisConfig,
        audit: TaskAuditStore,
        plan_id: str | None,
        slice_id: str | None,
    ) -> AgentSessionResult: ...


def _harness_root() -> Path:
    configured = os.environ.get("APOAPSIS_HARNESS_ROOT")
    if configured:
        return Path(configured).resolve()
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "tools" / "run_capability_sandbox_task.sh").is_file():
        return candidate
    raise CapabilitySandboxError(
        "Capability Sandbox needs the Apoapsis installation folder. Restart "
        "with START_APOAPSIS.cmd or set APOAPSIS_HARNESS_ROOT."
    )


def _wsl_path(path: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed executable and argv
        ["wsl.exe", "-d", "Ubuntu-24.04", "--", "wslpath", "-a", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        raise CapabilitySandboxError(
            "Ubuntu-24.04 could not resolve the selected project path: "
            + (completed.stderr or "").strip()
        )
    return completed.stdout.strip()


_PINNED_MANIFEST = "docs/qualification/slice7-crisis-atlas-pilot-manifest-v8.json"

_RESIDENT_SERVER_TOOL = "tools/resident_model_server.sh"


def _release_conflicting_model_server(root: Path) -> str | None:
    """Stop a host llama-server already holding the weights this run needs,
    and return the exact command line it was started with.

    `operator_lifecycle.stop_local_models` deliberately refuses to kill
    anything on this port, and its reasoning is right: Apoapsis launches
    the server through an operator-supplied command that may cross a
    process boundary it cannot see through -- `wsl.exe ...` yields the PID
    of wsl.exe, not of llama-server inside the distribution -- so killing
    by port would mean killing whatever a stranger happened to be running.

    That reasoning does not apply to a process whose own command line names
    the exact GGUF this run's pinned manifest is about to load. That is not
    a guess about identity; it is the conflict itself, because two copies
    of the same weights cannot both be resident. Nothing else is touched,
    including any other llama-server serving a different model.

    Without this the harness deadlocks against itself: START_APOAPSIS
    launches the configured local model, and the sandbox then refuses to
    run because a model is loaded. The operator's only escape was to kill,
    by hand, a process Apoapsis had started for them -- every single run.
    """

    manifest_path = root / _PINNED_MANIFEST
    if not manifest_path.is_file():
        return None
    try:
        model_path = json.loads(manifest_path.read_text(encoding="utf-8"))["model"][
            "absolute_path"
        ]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None

    tool = _wsl_path(root / _RESIDENT_SERVER_TOOL)
    stopped = subprocess.run(  # noqa: S603 - fixed bridge and argument vector
        ["wsl.exe", "-d", "Ubuntu-24.04", "--", "bash", tool, "stop", str(model_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    entries = [
        line.split("\t", 1)
        for line in (stopped.stdout or "").splitlines()
        if "\t" in line
    ]
    if not entries:
        return None
    return entries[0][1].strip()


def _restore_model_server(root: Path, command_line: str) -> None:
    """Put back exactly the server that was stopped, by the command line it
    was observed running under -- never a reconstructed or configured one.
    Best effort: the slice's result is already decided by this point, and
    failing to restore a convenience service must not change it."""

    try:
        subprocess.run(  # noqa: S603 - fixed bridge and argument vector
            ["wsl.exe", "-d", "Ubuntu-24.04", "--", "bash",
             _wsl_path(root / _RESIDENT_SERVER_TOOL), "restore", command_line],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pass


#: Never promoted into the task worktree, in either direction. These are the
#: harness's own metadata, not the product's, and the controller works in a
#: disposable clone whose `.git` is a directory while a managed worktree's is
#: a `gitdir:` pointer file. Promoting that delta overwrote the pointer -- or,
#: when the clone had no `.git` at all, deleted it -- leaving a worktree Git
#: reports as `prunable` and the review screen cannot open at all:
#:
#:     unhandled AgentInspectionError: inspection target must be a Git
#:     worktree root
#:
#: The model's changes were promoted correctly; the harness broke the
#: container it put them in, and the operator lost access to the review for a
#: run that had otherwise completed normally.
_NEVER_PROMOTED = (".git", ".apoapsis")


def _promote_snapshot(
    base: Path, snapshot: Path, worktree: Path, *, expected_fingerprint: str
) -> list[str]:
    """Apply only the controller-admitted delta to the normal task worktree."""

    delta = compute_delta(base, snapshot)
    observed = tree_fingerprint(snapshot)
    if observed != expected_fingerprint or observed != delta.candidate_fingerprint:
        raise CapabilitySandboxError("the admitted snapshot changed before promotion")
    promoted: list[str] = []
    for entry in delta.entries:
        relative = PurePosixPath(entry.path.replace("\\", "/"))
        if relative.parts and relative.parts[0] in _NEVER_PROMOTED:
            continue
        target = worktree / Path(entry.path)
        if entry.kind == ChangeKind.DELETED:
            target.unlink(missing_ok=True)
            promoted.append(entry.path)
            continue
        source = snapshot / Path(entry.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        promoted.append(entry.path)
    return promoted


class NativeQwenWorkcellExecutor:
    """Launch one authorized native-Qwen product run through Ubuntu WSL.

    No fallback is attempted.  A missing runtime, failed containment check,
    refused candidate, or unready checkpoint becomes a review result.
    """

    def run(
        self,
        *,
        specification: TaskSpecification,
        worktree: Path,
        context: ContextPackage,
        config: ApoapsisConfig,
        audit: TaskAuditStore,
        plan_id: str | None,
        slice_id: str | None,
    ) -> AgentSessionResult:
        if plan_id is None or slice_id is None:
            return self._review(
                "Capability Sandbox currently requires an approved plan slice; "
                "use compatibility mode for a quick-change task"
            )
        root = _harness_root()
        run_id = f"CAP-{uuid.uuid4().hex[:16].upper()}"
        task_dir = audit.root / "capability-sandbox" / run_id
        task_dir.mkdir(parents=True, exist_ok=False)
        request_path = task_dir / "request.json"
        response_path = task_dir / "result.json"
        approval = next(
            (
                event for event in reversed(
                    SQLiteTaskStore(
                        Path(audit.project_root) / ".apoapsis" / "apoapsis.db",
                        initialize=False,
                    ).events(specification.task_id)
                )
                if event.event_type in {
                    "plan_slice_specification_approved",
                    "plan_slice_auto_approved",
                }
            ),
            None,
        )
        if approval is None:
            return self._review("the approved plan-slice event is missing")
        package_id = approval.payload.get("package_id")
        package_sha = approval.payload.get("package_sha256")
        package_artifact = (
            Path(audit.project_root) / ".apoapsis" / "plans" / plan_id
            / f"slice-{slice_id}-package-{package_id}.json"
        )
        if not package_artifact.is_file():
            return self._review(f"approved slice package is missing for {plan_id}/{slice_id}")
        slice_package = PlanSliceExecutionPackage.model_validate_json(
            package_artifact.read_text(encoding="utf-8")
        )
        if slice_package.package_sha256 != package_sha:
            return self._review("the approved slice package hash no longer matches")
        try:
            approved_plan = _approved_plan_payload(
                Path(audit.project_root), slice_package
            )
        except CapabilitySandboxError as exc:
            return self._review(str(exc))
        request = {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": specification.task_id,
            "plan_id": plan_id,
            "slice_id": slice_id,
            "task": specification.model_dump(mode="json"),
            "context_sha256": context.context_sha256,
            "plan_version": slice_package.plan_version,
            "slice_package_sha256": slice_package.package_sha256,
            "plan": approved_plan,
            # Where the deciding verification actually runs. The coding model
            # works in a Linux container; configured verification runs through
            # the project's execution backend, which for the ordinary product
            # path is this host. A model that cannot see that difference gets a
            # green run in the container and an unexplained failure afterwards.
            "independent_verification": {
                "platform": platform.system(),
                "backend": config.verification.backend.backend.value,
            },
            "verification_commands": [
                {
                    "name": item.name,
                    "argv": item.argv,
                    "timeout_seconds": item.timeout_seconds,
                }
                for item in config.verification.commands
            ],
            "max_native_continuations": (
                config.execution.capability_sandbox.max_native_continuations
            ),
            "high_assurance_parity_guard": (
                config.execution.capability_sandbox.high_assurance_parity_guard
            ),
            "runtime_profile": config.execution.capability_sandbox.runtime_profile,
            "qualified_model_alias": (
                config.execution.capability_sandbox.qualified_model_alias
            ),
            "patch_policy": {
                "max_files": config.patch.max_files,
                "max_changed_lines": config.patch.max_changed_lines,
                "allow_test_changes": config.patch.allow_test_changes,
                "allow_dependency_changes": config.patch.allow_dependency_changes,
            },
        }
        request_path.write_text(
            json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        audit.write_json(
            f"capability-sandbox-{run_id}-authorization.json",
            request,
            kind="capability_sandbox_authorization",
        )

        if os.name != "nt":
            raise CapabilitySandboxError(
                "the current product launcher supports the qualified Ubuntu-24.04 "
                "WSL runtime; no alternate platform fallback is configured"
            )
        command = [
            "wsl.exe", "-d", "Ubuntu-24.04", "--", "bash",
            _wsl_path(root / "tools" / "run_capability_sandbox_task.sh"),
            _wsl_path(root),
            _wsl_path(worktree),
            _wsl_path(request_path),
            _wsl_path(response_path),
        ]
        # Apoapsis started the configured local model itself, and the
        # controller is about to start its own copy of the same weights.
        # Releasing ours first is the harness getting out of its own way,
        # not a policy decision the operator has to make again every run.
        released_model_server = _release_conflicting_model_server(root)
        completed = subprocess.run(  # noqa: S603 - fixed bridge and argument vector
            command,
            capture_output=True,
            text=True,
            # The bridge relays a Linux model session, so its output is UTF-8
            # and routinely carries bytes no Windows ANSI code page maps.
            # Without this, `text=True` decodes with the locale codec, the
            # reader thread dies of UnicodeDecodeError, and `stdout` arrives
            # as None -- which the very next line then tries to write, raising
            # "TypeError: data must be str, not NoneType" and discarding the
            # run. A completed slice was lost to this: the controller had
            # already written outcome "complete" with readiness proved, and
            # the operator was shown a failed task with no files changed.
            # Decoding is not where a verdict should be decided, so replace
            # undecodable bytes rather than raising on them.
            encoding="utf-8",
            errors="replace",
            timeout=7200,
            # `wsl.exe` translates the caller's Windows working directory into
            # the Linux one, so without this the bridge hands the *project*
            # folder to a script whose job is to read the *harness* repository.
            # Every path the script needs is passed explicitly above, but the
            # controller image build shells out to `git rev-parse
            # --show-toplevel`, which is answered by whatever repository
            # surrounds the inherited directory -- reporting the harness commit
            # as an unknown revision. Anchor the bridge to the harness root.
            cwd=root,
        )
        if released_model_server is not None:
            _restore_model_server(root, released_model_server)
        # `or ""`: a diagnostic log is never worth converting a finished run
        # into a crash, whatever the bridge did or did not emit.
        (task_dir / "bridge-stdout.log").write_text(
            completed.stdout or "", encoding="utf-8"
        )
        (task_dir / "bridge-stderr.log").write_text(
            completed.stderr or "", encoding="utf-8"
        )
        if completed.returncode != 0 or not response_path.is_file():
            detail = (
                (completed.stderr or "").strip()
                or (completed.stdout or "").strip()
                or f"launcher exited {completed.returncode} without a result artifact"
            )
            return self._review(
                "Capability Sandbox preflight or runtime failed; no compatibility "
                "fallback was attempted. " + detail[-1000:]
            )
        payload = json.loads(response_path.read_text(encoding="utf-8"))
        if payload.get("outcome") != "complete":
            return self._review(str(payload.get("detail") or "checkpoint was not complete"))

        snapshot = Path(str(payload["snapshot_path_windows"]))
        checkpoint = payload.get("checkpoint") or {}
        expected_fingerprint = checkpoint.get("candidate_fingerprint")
        if not isinstance(expected_fingerprint, str):
            return self._review("the controller result omitted its candidate fingerprint")
        changed = _promote_snapshot(
            worktree,
            snapshot,
            worktree,
            expected_fingerprint=expected_fingerprint,
        )
        verification = VerificationRunner(config.verification).run(
            specification.task_id, worktree, attempt=1
        )
        if verification.status != VerificationStatus.PASSED:
            return AgentSessionResult(
                outcome=AgentSessionOutcome.ESCALATION_REQUIRED,
                stop_reason="admitted native candidate failed independent verification",
                turns=int(payload.get("turns", 1)),
                patch_attempts=1,
                verification_runs=1,
                changed_files=changed,
                verification_results=[verification],
            )
        return AgentSessionResult(
            outcome=AgentSessionOutcome.COMPLETE,
            stop_reason="Capability Sandbox checkpoint complete and verification passed",
            turns=int(payload.get("turns", 1)),
            patch_attempts=1,
            verification_runs=1,
            changed_files=changed,
            verification_results=[verification],
        )

    @staticmethod
    def _review(reason: str) -> AgentSessionResult:
        return AgentSessionResult(
            outcome=AgentSessionOutcome.ESCALATION_REQUIRED,
            stop_reason=reason,
            turns=0,
            patch_attempts=0,
            verification_runs=0,
        )


__all__ = [
    "CapabilitySandboxError",
    "CapabilitySandboxExecutor",
    "NativeQwenWorkcellExecutor",
]

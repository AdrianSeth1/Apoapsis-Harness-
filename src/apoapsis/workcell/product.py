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
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import ApoapsisConfig
from apoapsis.context.compiler import ContextPackage
from apoapsis.models.telemetry import RelayObservedModelUsage
from apoapsis.specification.schema import TaskSpecification
from apoapsis.verification.runner import VerificationRunner
from apoapsis.verification.results import VerificationStatus
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workcell.delta import (
    EXCLUDED_METADATA_NAMES,
    ChangeKind,
    compute_delta,
    tree_fingerprint,
)
from apoapsis.workcell.orientation import SliceContribution, build_orientation_brief
from apoapsis.workcell.parity import select_parity, slice_position


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



def _project_relative(project_root: Path, path: Path) -> str | None:
    """The path as a report reader will look for it, or `None` if unwritten.

    Reported relative to the project because absolute host paths in a report
    are unusable to anyone but the machine that produced it.
    """

    if not path.is_file():
        return None
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return None


def _non_negative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def _model_usage(
    payload: dict, *, series_artifact: str | None
) -> RelayObservedModelUsage | None:
    """Read the controller's observed usage out of a sandbox result.

    Returns `None` when the result carries no `model_usage` block at all --
    a result written by an older controller. That is a genuine absence and is
    reported as one; substituting zeros would be indistinguishable from a run
    that spent nothing, which is exactly the reading this whole change exists
    to make impossible.
    """

    block = payload.get("model_usage")
    if not isinstance(block, dict):
        return None
    return RelayObservedModelUsage(
        calls=_non_negative_int(block.get("calls")),
        exchanges_observed=_non_negative_int(payload.get("relay_requests")),
        input_tokens=_non_negative_int(block.get("input_tokens")),
        output_tokens=_non_negative_int(block.get("output_tokens")),
        cached_input_tokens=_non_negative_int(block.get("cached_input_tokens")),
        peak_input_tokens=_non_negative_int(block.get("peak_input_tokens")),
        series_artifact=series_artifact,
    )


def _slice_contributions(
    project_root: Path, plan: dict, plan_id: str, slice_id: str
) -> list[SliceContribution]:
    """What each earlier slice of this plan actually put in the repository.

    Read from artifacts the harness wrote itself: the completed slice's task
    report for the files, and its last checkpoint record for the additions its
    own witnesses proved. Nothing here is inferred from the code, and nothing
    is asked of a model -- a slice's contribution is a fact the harness already
    established when it admitted that slice.

    Best effort by design. A missing or unreadable artifact costs one slice's
    row in the brief; it must never cost the run, because orientation is a
    convenience and the work is authorised by the package regardless.
    """

    order = [item.get("slice_id") for item in plan.get("slices", [])]
    try:
        position = order.index(slice_id)
    except ValueError:
        return []
    titles = {
        item.get("slice_id"): item.get("title", "")
        for item in plan.get("slices", [])
    }

    try:
        records = PlanSliceExecutionStore(
            project_root / ".apoapsis" / "plan-slice-executions.db",
            initialize=False,
        ).list_for_plan(plan_id)
    except Exception:  # noqa: BLE001 - see the docstring: never fail the run
        return []

    by_slice = {item.slice_id: item for item in records}
    contributions: list[SliceContribution] = []
    for earlier in order[:position]:
        record = by_slice.get(earlier)
        if record is None or record.task_id is None:
            continue
        task_directory = project_root / ".apoapsis" / "tasks" / record.task_id
        paths: list[str] = []
        report = task_directory / "report.json"
        if not report.is_file():
            continue
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Gated on the task's own reported outcome, not on the slice record's
        # status. Observed in `test project 6`: all four finished slices sit at
        # `approved` in `plan-slice-executions.db` while their reports read
        # `complete`, so trusting the record's status would have produced an
        # empty brief on every real project -- silently, which is the worst way
        # for an optimisation to not work. An incomplete slice's files are not
        # inherited state, and saying they are would describe a repository that
        # does not exist.
        if payload.get("outcome") != "complete":
            continue
        # Reports written before ADR 0102 list `.git` in `files_changed`.
        # Those reports are left exactly as they were written -- they are the
        # record of what was observed at the time -- so the filtering happens
        # here, at the point of reuse, rather than by rewriting history.
        paths = [
            item
            for item in payload.get("files_changed", [])
            if isinstance(item, str)
            and item.split("/", 1)[0] not in EXCLUDED_METADATA_NAMES
        ]
        names: list[str] = []
        checkpoints = sorted(
            task_directory.glob(
                "capability-sandbox/*/evidence/*/checkpoint-*/checkpoint.json"
            )
        )
        if checkpoints:
            try:
                record_payload = json.loads(
                    checkpoints[-1].read_text(encoding="utf-8")
                )
                names = sorted(
                    {
                        str(unit.get("name"))
                        for unit in (record_payload.get("behaviour_units") or [])
                        if unit.get("name")
                    }
                )
            except (OSError, json.JSONDecodeError):
                names = []
        if paths or names:
            contributions.append(
                SliceContribution(
                    slice_id=earlier,
                    title=str(titles.get(earlier) or ""),
                    paths=paths,
                    behaviour_names=names,
                )
            )
    return contributions


def _slice_integration_contracts(plan: dict, slice_id: str) -> list[str]:
    """The plan's own interface statements for this slice, verbatim."""

    target = next(
        (item for item in plan.get("slices", []) if item.get("slice_id") == slice_id),
        None,
    )
    if target is None:
        return []
    wanted = set(target.get("integration_contract_ids") or [])
    lines = [
        f"{item['contract_id']}: {item['interface']} — {item['data_flow']}"
        for item in plan.get("integration_contracts", [])
        if item.get("contract_id") in wanted
    ]
    # The slice's own free-text interface contracts are the planner's words
    # about this slice specifically, so they belong here too.
    lines.extend(str(item) for item in (target.get("interface_contracts") or []))
    return lines



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
        sandbox_config = config.execution.capability_sandbox
        parity = select_parity(
            mode=sandbox_config.parity_mode,
            slice_position=slice_position(approved_plan, slice_id),
            sample_every=sandbox_config.parity_sample_every,
        )
        # Built here, on the host, because this is the side that can read what
        # earlier slices did: their task reports and checkpoint records live in
        # this project's audit tree, and the controller sees only this request.
        orientation = build_orientation_brief(
            worktree,
            contributions=_slice_contributions(
                Path(audit.project_root), approved_plan, plan_id, slice_id
            ),
            integration_contracts=_slice_integration_contracts(
                approved_plan, slice_id
            ),
            commands=[
                " ".join(item.argv) for item in config.verification.commands
            ],
            focus_paths=[
                str(item)
                for item in (
                    next(
                        (
                            entry
                            for entry in approved_plan.get("slices", [])
                            if entry.get("slice_id") == slice_id
                        ),
                        {},
                    ).get("suggested_paths")
                    or []
                )
            ],
        )
        request = {
            "schema_version": "1.0",
            "orientation": orientation,
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
            # Decided here, per slice, from the plan's own ordering (ADR 0108).
            # The controller is told whether *this* slice pairs, and why, so it
            # never has to re-derive a policy decision from configuration it
            # cannot see.
            "high_assurance_parity_guard": parity.run_control_arm,
            "parity_selection": parity.model_dump(mode="json"),
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
        usage = _model_usage(
            payload,
            series_artifact=_project_relative(
                Path(audit.project_root),
                task_dir / "evidence" / "sandbox" / "model-usage-series.json",
            ),
        )
        if payload.get("outcome") != "complete":
            return self._review(
                str(payload.get("detail") or "checkpoint was not complete"),
                model_usage=usage,
            )

        snapshot = Path(str(payload["snapshot_path_windows"]))
        checkpoint = payload.get("checkpoint") or {}
        expected_fingerprint = checkpoint.get("candidate_fingerprint")
        if not isinstance(expected_fingerprint, str):
            return self._review(
                "the controller result omitted its candidate fingerprint",
                model_usage=usage,
            )
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
                model_usage=usage,
            )
        return AgentSessionResult(
            outcome=AgentSessionOutcome.COMPLETE,
            stop_reason="Capability Sandbox checkpoint complete and verification passed",
            turns=int(payload.get("turns", 1)),
            patch_attempts=1,
            verification_runs=1,
            changed_files=changed,
            verification_results=[verification],
            model_usage=usage,
        )

    @staticmethod
    def _review(
        reason: str, *, model_usage: RelayObservedModelUsage | None = None
    ) -> AgentSessionResult:
        # A run that stopped short still spent tokens, and a review result that
        # dropped them would put the cost of every unsuccessful attempt at
        # zero -- the most misleading direction for this number to be wrong in.
        return AgentSessionResult(
            outcome=AgentSessionOutcome.ESCALATION_REQUIRED,
            stop_reason=reason,
            turns=0,
            patch_attempts=0,
            verification_runs=0,
            model_usage=model_usage,
        )


__all__ = [
    "CapabilitySandboxError",
    "CapabilitySandboxExecutor",
    "NativeQwenWorkcellExecutor",
]

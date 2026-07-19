from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from apoapsis.config import ApoapsisConfig
from apoapsis.evaluation.schemas import (
    HeldOutOracleResult,
    OracleStatus,
)
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.repository.git import GitRepository
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import (
    VerificationCommand,
    VerificationRunner,
)


@dataclass(frozen=True)
class HeldOutOracleDefinition:
    oracle_id: str
    version: str
    source_path: Path
    withheld_relative_path: str


def assert_oracle_withheld(
    fixture_root: Path, definition: HeldOutOracleDefinition
) -> None:
    """Fail before any model call if oracle source entered the fixture."""

    root = Path(fixture_root).resolve()
    declared = (root / definition.withheld_relative_path).resolve()
    try:
        declared.relative_to(root)
    except ValueError as exc:
        raise ValueError("held-out oracle path escapes the evaluation fixture") from exc
    if declared.exists() or declared.is_symlink():
        raise ValueError(
            "held-out oracle is present in the agent-visible evaluation fixture"
        )
    source_digest = hashlib.sha256(Path(definition.source_path).read_bytes()).hexdigest()
    repository = GitRepository(root)
    tracked = repository.run(["ls-files", "-z"]).stdout.split("\0")
    for relative in tracked:
        if not relative:
            continue
        candidate = root / relative
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == source_digest:
            raise ValueError(
                "held-out oracle contents are present in a tracked fixture file"
            )


def run_held_out_oracle(
    report: FinalTaskReport,
    config: ApoapsisConfig,
    definition: HeldOutOracleDefinition,
) -> HeldOutOracleResult:
    """Run a harness-owned oracle only after normal completion of one task.

    The source is copied into the completed worktree after all model calls, so
    neither its filename nor its contents can enter repository context. The
    oracle reuses the configured execution backend and fixed interpreter; it
    grants the model no command-selection or retry authority.
    """

    source_sha256 = hashlib.sha256(Path(definition.source_path).read_bytes()).hexdigest()
    if report.outcome != TaskOutcome.COMPLETE:
        return HeldOutOracleResult(
            oracle_id=definition.oracle_id,
            oracle_version=definition.version,
            source_sha256=source_sha256,
            status=OracleStatus.NOT_RUN,
            reason="normal verification did not claim task completion",
        )
    if report.worktree_path is None or not Path(report.worktree_path).is_dir():
        return HeldOutOracleResult(
            oracle_id=definition.oracle_id,
            oracle_version=definition.version,
            source_sha256=source_sha256,
            status=OracleStatus.INFRASTRUCTURE_ERROR,
            reason="completed task report has no usable worktree",
        )
    return run_held_out_oracle_against_worktree(
        Path(report.worktree_path),
        config,
        definition,
        task_id=report.task_id,
        attempt_offset=len(report.verification_results) + 100,
    )


def run_held_out_oracle_against_worktree(
    worktree: str | Path,
    config: ApoapsisConfig,
    definition: HeldOutOracleDefinition,
    *,
    task_id: str,
    attempt_offset: int = 100,
) -> HeldOutOracleResult:
    """The same harness-owned oracle mechanics as `run_held_out_oracle`, but
    against an arbitrary worktree path rather than one task's own completed
    report -- used by the planning-comparison harness (ADR 0028) to run the
    oracle once against a planned condition's final, merged repository
    state, which is not any single task's own worktree."""

    source_sha256 = hashlib.sha256(Path(definition.source_path).read_bytes()).hexdigest()
    worktree = Path(worktree).resolve()
    if not worktree.is_dir():
        return HeldOutOracleResult(
            oracle_id=definition.oracle_id,
            oracle_version=definition.version,
            source_sha256=source_sha256,
            status=OracleStatus.INFRASTRUCTURE_ERROR,
            reason="no usable worktree was supplied",
        )
    if not config.verification.commands:
        return HeldOutOracleResult(
            oracle_id=definition.oracle_id,
            oracle_version=definition.version,
            source_sha256=source_sha256,
            status=OracleStatus.INFRASTRUCTURE_ERROR,
            reason="no configured verification command supplies an interpreter",
        )

    source = Path(definition.source_path).resolve()
    oracle_name = f".apoapsis_holdout_{source_sha256[:12]}.py"
    oracle_path = worktree / oracle_name
    if oracle_path.exists() or oracle_path.is_symlink():
        return HeldOutOracleResult(
            oracle_id=definition.oracle_id,
            oracle_version=definition.version,
            source_sha256=source_sha256,
            status=OracleStatus.INFRASTRUCTURE_ERROR,
            reason="reserved held-out oracle path already exists",
        )

    started = time.monotonic()
    result = None
    error: str | None = None
    cleanup_error: str | None = None
    try:
        shutil.copy2(source, oracle_path)
        interpreter = config.verification.commands[0].argv[0]
        command = VerificationCommand(
            name="held-out-correctness-oracle",
            category="held_out_oracle",
            argv=[interpreter, oracle_name],
            timeout_seconds=min(
                config.verification.commands[0].timeout_seconds, 120.0
            ),
            required=True,
        )
        oracle_config = config.verification.model_copy(
            update={"commands": [command], "stop_on_failure": True}
        )
        result = VerificationRunner(oracle_config).run(
            task_id,
            worktree,
            attempt=attempt_offset,
        )
    except Exception as exc:  # noqa: BLE001 - normalized as oracle infrastructure
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            oracle_path.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"

    duration = time.monotonic() - started
    if error is not None or cleanup_error is not None or result is None:
        reasons = [item for item in (error, cleanup_error) if item]
        return HeldOutOracleResult(
            oracle_id=definition.oracle_id,
            oracle_version=definition.version,
            source_sha256=source_sha256,
            status=OracleStatus.INFRASTRUCTURE_ERROR,
            duration_seconds=duration,
            verification_result=result,
            reason="; ".join(reasons) or "oracle did not produce a result",
        )
    if result.status == VerificationStatus.PASSED:
        status = OracleStatus.PASSED
        reason = None
    elif result.status in {VerificationStatus.FAILED, VerificationStatus.TIMED_OUT}:
        status = OracleStatus.FAILED
        reason = "held-out correctness behavior did not pass"
    else:
        status = OracleStatus.INFRASTRUCTURE_ERROR
        reason = "held-out command could not execute reliably"
    return HeldOutOracleResult(
        oracle_id=definition.oracle_id,
        oracle_version=definition.version,
        source_sha256=source_sha256,
        status=status,
        duration_seconds=duration,
        verification_result=result,
        reason=reason,
    )

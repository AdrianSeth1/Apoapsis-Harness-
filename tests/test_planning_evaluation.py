from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apoapsis.architect.schema import ArchitecturePlan, ImplementationSlice
from apoapsis.architect.store import SQLitePlanStore
from apoapsis.architect.slice_store import PlanSliceExecutionStore
from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    ApoapsisConfig,
    CompletionPolicy,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ProviderPricing,
)
from apoapsis.evaluation.fixture import prepare_fixture_repository
from apoapsis.evaluation.oracle import HeldOutOracleDefinition
from apoapsis.evaluation.planning_aggregate import summarize_planning_comparisons
from apoapsis.evaluation.planning_harness import (
    run_monolithic_condition,
    run_planned_condition,
)
from apoapsis.evaluation.planning_schemas import (
    MonolithicConditionResult,
    PlannedConditionResult,
    PlannerMethod,
    PlannerProvenance,
    PlanningComparisonReport,
    SliceAttemptResult,
)
from apoapsis.evaluation.schemas import EvalEvidenceKind, MetricStatus, OracleStatus
from apoapsis.execution.operation_store import ExecutionOperationStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import FinalTaskReport, TaskOutcome
from apoapsis.specification.schema import AcceptanceCriterion, SourceKind
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.engine import SQLiteTaskStore
from tests.architect_helpers import make_plan, make_slice
from tests.fakes import FakeModelProvider
from tests.test_agent_loop import action

_FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "download-service-v2"
_HOLDOUT_RELATIVE_PATH = "tests/test_v2_holdout_acceptance.py"

_TARGET_JOBS = '''from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class JobRecord:
    offset: int = 0
    expected_checksum: str | None = None
    attempt_count: int = 0
    transferred_bytes: int = 0
    state: JobState = JobState.PENDING
    last_error: str | None = None


class JobStore:
    """In-memory stand-in for persisted download-job bookkeeping."""

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}

    def get_record(self, url: str) -> JobRecord:
        return self._records.setdefault(url, JobRecord())

    def get_offset(self, url: str) -> int:
        return self.get_record(url).offset

    def set_offset(self, url: str, offset: int) -> None:
        self.get_record(url).offset = offset

    def record_attempt(self, url: str) -> int:
        record = self.get_record(url)
        record.attempt_count += 1
        return record.attempt_count

    def record_progress(self, url: str, offset: int, transferred_bytes: int) -> None:
        record = self.get_record(url)
        record.offset = offset
        record.transferred_bytes = transferred_bytes
        record.state = JobState.IN_PROGRESS

    def set_expected_checksum(self, url: str, checksum: str) -> None:
        self.get_record(url).expected_checksum = checksum

    def mark_state(self, url: str, state: JobState, *, error: str | None = None) -> None:
        record = self.get_record(url)
        record.state = state
        record.last_error = error
'''

_TARGET_DOWNLOADER = '''from __future__ import annotations

import time
from pathlib import Path
from typing import Callable


class Downloader:
    def __init__(
        self,
        transport: object,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.5,
    ) -> None:
        self.transport = transport
        self._sleep = sleep
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds

    def download(
        self,
        url: str,
        destination: Path,
        *,
        resume_offset: int = 0,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        headers = {"Range": f"bytes={resume_offset}-"} if resume_offset else {}
        response = self._get_with_retry(url, headers)
        destination.parent.mkdir(parents=True, exist_ok=True)
        resumed = resume_offset > 0 and response.status_code == 206
        mode = "ab" if resumed else "wb"
        offset = resume_offset if resumed else 0
        transferred = 0
        with destination.open(mode) as handle:
            for chunk in response.iter_chunks():
                handle.write(chunk)
                offset += len(chunk)
                transferred += len(chunk)
                if on_progress is not None:
                    on_progress(offset, transferred)
        return offset

    def _get_with_retry(self, url: str, headers: dict[str, str]):
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.transport.get(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - retried deterministically
                last_error = exc
                if attempt == self.max_attempts:
                    raise
                self._sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
        raise last_error  # pragma: no cover - unreachable
'''

# Same as _TARGET_DOWNLOADER except the progress callback reports only the
# current chunk's own length instead of cumulative offset/transferred --
# invisible to a single-chunk dev test (the two coincide when there is only
# one chunk) but caught by the held-out oracle's multi-chunk case.
_BUGGY_DOWNLOADER_DOUBLE_COUNTS_PROGRESS = _TARGET_DOWNLOADER.replace(
    "on_progress(offset, transferred)", "on_progress(len(chunk), len(chunk))"
)

_TARGET_SERVICE = '''from __future__ import annotations

import hashlib
from pathlib import Path

from .downloader import Downloader
from .jobs import JobState, JobStore


class ChecksumMismatchError(Exception):
    pass


class DownloadService:
    def __init__(self, transport: object, jobs: JobStore) -> None:
        self.jobs = jobs
        self.downloader = Downloader(transport)

    def run(self, url: str, destination: Path) -> int:
        record = self.jobs.get_record(url)
        self.jobs.record_attempt(url)
        try:
            downloaded = self.downloader.download(
                url,
                destination,
                resume_offset=record.offset,
                on_progress=lambda offset, transferred: self.jobs.record_progress(
                    url, offset, transferred
                ),
            )
        except Exception as exc:
            self.jobs.mark_state(
                url, JobState.FAILED, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        if record.expected_checksum is not None:
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual != record.expected_checksum:
                self.jobs.mark_state(url, JobState.FAILED, error="checksum mismatch")
                raise ChecksumMismatchError(
                    f"expected {record.expected_checksum}, got {actual}"
                )
        self.jobs.mark_state(url, JobState.COMPLETE)
        return downloaded
'''


class PlanningEvaluationTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.output_root = Path(self.temporary_directory.name)

    def _v2_config(self) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1", model="fake-coder-v1"
                )
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=200),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT,
                route=AgentRoute.LOCAL_ONLY,
                completion_policy=CompletionPolicy.STRICT,
                agent=AgentLoopConfig(
                    max_turns=8,
                    max_patch_attempts=6,
                    max_verification_runs=6,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
            ),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="v2-jobs-tests",
                        category="tests",
                        argv=[sys.executable, "-m", "unittest", "tests.test_jobs_contract", "-v"],
                        timeout_seconds=30,
                        # Slice A has no dependencies and always runs first;
                        # see ADR 0028 for why this is the one command that
                        # can safely satisfy the "at least one required
                        # command" floor without blocking any other slice.
                        required=True,
                        acceptance=True,
                    ),
                    VerificationCommand(
                        name="v2-downloader-tests",
                        category="tests",
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "tests.test_resilient_downloader",
                            "-v",
                        ],
                        timeout_seconds=30,
                        required=False,
                        acceptance=True,
                    ),
                    VerificationCommand(
                        name="v2-service-tests",
                        category="acceptance",
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "tests.test_service_integration_visible",
                            "-v",
                        ],
                        timeout_seconds=30,
                        required=False,
                        acceptance=True,
                    ),
                ]
            ),
        )

    def _oracle(self) -> HeldOutOracleDefinition:
        return HeldOutOracleDefinition(
            oracle_id="download-service-v2-holdout-v1",
            version="1.0",
            source_path=_FIXTURE / _HOLDOUT_RELATIVE_PATH,
            withheld_relative_path=_HOLDOUT_RELATIVE_PATH,
        )

    def _fixture_copy(self, name: str) -> Path:
        destination = self.output_root / name / "download-service-v2"
        prepare_fixture_repository(
            _FIXTURE, destination, excluded_relative_files=[_HOLDOUT_RELATIVE_PATH]
        )
        return destination

    @staticmethod
    def _provider(outputs: list[str]) -> InstrumentedModelProvider:
        return InstrumentedModelProvider(FakeModelProvider(outputs), ProviderPricing())

    @staticmethod
    def _read(path: Path, relative: str) -> str:
        return (path / relative).read_text(encoding="utf-8")


def _v2_plan(*, downloader_dependency_breaks_c: bool = False) -> ArchitecturePlan:
    slices: list[ImplementationSlice] = [
        make_slice(
            slice_id="SLICE-A",
            acceptance_criterion_ids=["AC-JOBS"],
            verification_commands=["v2-jobs-tests"],
            suggested_paths=["src/download_service_v2/jobs.py"],
        ),
        make_slice(
            slice_id="SLICE-B",
            acceptance_criterion_ids=["AC-DL"],
            verification_commands=["v2-downloader-tests"],
            suggested_paths=["src/download_service_v2/downloader.py"],
        ),
        make_slice(
            slice_id="SLICE-C",
            dependencies=["SLICE-A", "SLICE-B"],
            acceptance_criterion_ids=["AC-SVC"],
            verification_commands=["v2-service-tests"],
            suggested_paths=["src/download_service_v2/service.py"],
        ),
    ]
    return make_plan(
        slices=slices,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-JOBS",
                text="Job records track attempts, progress, checksum, and state.",
                source=SourceKind.USER,
                source_reference="idea",
                verification_method="v2-jobs-tests",
            ),
            AcceptanceCriterion(
                id="AC-DL",
                text="Downloads resume, retry with backoff, and report progress.",
                source=SourceKind.USER,
                source_reference="idea",
                verification_method="v2-downloader-tests",
            ),
            AcceptanceCriterion(
                id="AC-SVC",
                text="The integrated service verifies a checksum before completion.",
                source=SourceKind.USER,
                source_reference="idea",
                verification_method="v2-service-tests",
            ),
        ],
    )


class MonolithicConditionTests(PlanningEvaluationTestsBase):
    def test_monolithic_condition_completes_and_passes_held_out_oracle(self) -> None:
        fixture = self._fixture_copy("monolithic-complete")
        config = self._v2_config()
        spec_payload = _monolithic_specification_response()
        fake = FakeModelProvider(
            [
                spec_payload,
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                action(
                    "replace_text",
                    path="src/download_service_v2/downloader.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/downloader.py"),
                    new_text=_TARGET_DOWNLOADER,
                ),
                action(
                    "replace_text",
                    path="src/download_service_v2/service.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/service.py"),
                    new_text=_TARGET_SERVICE,
                ),
                action("submit_for_verification"),
            ]
        )
        _inject_task_id(fake)
        result = run_monolithic_condition(
            fixture,
            config,
            InstrumentedModelProvider(fake, ProviderPricing()),
            task_text=(
                "Add resilient, checksum-verified resumable downloads.\n"
                "Preserve the current public API."
            ),
            scenario_id="download-service-v2",
            scenario_version="1.0",
            evidence_kind=EvalEvidenceKind.DETERMINISTIC_FAKE,
            held_out_oracle=self._oracle(),
        )
        self.assertIsNotNone(result.report)
        self.assertEqual(result.report.outcome, TaskOutcome.COMPLETE)
        self.assertIsNotNone(result.held_out_oracle)
        self.assertEqual(result.held_out_oracle.status, OracleStatus.PASSED)

    def test_oracle_is_absent_from_the_agent_visible_copy(self) -> None:
        fixture = self._fixture_copy("oracle-absence")
        self.assertFalse((fixture / _HOLDOUT_RELATIVE_PATH).exists())


class PlannedConditionTests(PlanningEvaluationTestsBase):
    def _approve_plan(self, root: Path):
        plan_store = SQLitePlanStore(root / ".apoapsis" / "architect-plans.db")
        task_store = SQLiteTaskStore(root / ".apoapsis" / "apoapsis.db")
        slice_store = PlanSliceExecutionStore(root / ".apoapsis" / "plan-slice-executions.db")
        operation_store = ExecutionOperationStore(root / ".apoapsis" / "execution-operations.db")
        from apoapsis.architect.package import build_planner_request_package
        from apoapsis.architect.validation import validate_plan
        from apoapsis.architect.audit import write_package_artifact

        config = self._v2_config()
        package = build_planner_request_package(root, "Add resilient downloads.", config)
        write_package_artifact(root, package)
        plan = _v2_plan()
        record = plan_store.create_plan("PLAN-V2TEST", package.package_id, package.idea_text, plan)
        findings = validate_plan(
            plan,
            configured_verification_commands={c.name for c in config.verification.commands},
            ceilings=config.architect.ceilings,
        )
        self.assertFalse(any(f.severity.value == "error" for f in findings))
        from apoapsis.architect.schema import PlanValidationResult

        result = PlanValidationResult(
            plan_id=record.plan_id, plan_version=record.version, valid=True, findings=findings
        )
        record = plan_store.record_validation(
            record.plan_id, result, expected_version=record.version
        )
        record = plan_store.approve_plan(record.plan_id, expected_version=record.version)
        return record, plan_store, task_store, slice_store, operation_store, config

    def _planner(self, plan_id: str, plan_version: int, package_id: str) -> PlannerProvenance:
        return PlannerProvenance(
            package_id=package_id,
            plan_id=plan_id,
            plan_version=plan_version,
            request_package_sha256="0" * 64,
            planner_model="framework-fake-test-planner",
            planner_method=PlannerMethod.FRAMEWORK_FAKE,
            planner_tokens_status=MetricStatus.UNMEASURED,
            reason="deterministic fake-provider test, never live evidence",
        )

    def test_all_three_slices_complete_in_order_and_oracle_passes(self) -> None:
        root = self._fixture_copy("planned-complete")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        planner = self._planner(record.plan_id, record.version, record.package_id)

        provider_a = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        provider_b = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/downloader.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/downloader.py"),
                    new_text=_TARGET_DOWNLOADER,
                ),
                action("run_check", command_name="v2-downloader-tests"),
                # "v2-jobs-tests" is the one globally required command
                # (ADR 0028); the required-check floor is only satisfied by
                # commands actually executed at the current worktree digest
                # during this session, so every slice must re-run it even
                # when jobs.py is unrelated to its own work -- it already
                # passes here since Slice A's fix is merged in.
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        provider_c = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/service.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/service.py"),
                    new_text=_TARGET_SERVICE,
                ),
                action("run_check", command_name="v2-service-tests"),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )

        with patch(
            "apoapsis.execution.operation_service._build_providers",
            side_effect=[
                (provider_a, provider_a, None),
                (provider_b, provider_b, None),
                (provider_c, provider_c, None),
            ],
        ):
            result = run_planned_condition(
                root,
                plan_store,
                slice_store,
                task_store,
                operation_store,
                record.plan_id,
                expected_plan_version=record.version,
                config=config,
                planner=planner,
                scenario_id="download-service-v2",
                scenario_version="1.0",
                held_out_oracle=self._oracle(),
            )

        self.assertTrue(result.all_slices_complete)
        self.assertIsNone(result.stopped_at_slice_id)
        self.assertEqual([item.slice_id for item in result.slices], ["SLICE-A", "SLICE-B", "SLICE-C"])
        self.assertTrue(all(item.attempted for item in result.slices))
        self.assertIsNotNone(result.held_out_oracle)
        self.assertEqual(result.held_out_oracle.status, OracleStatus.PASSED)
        self.assertFalse(result.integration_failure)

    def test_dependent_slice_is_never_packaged_before_its_dependencies_merge(self) -> None:
        # Regression guard: SLICE-C's own dependency-evidence check inside
        # `package_slice` (ADR 0027) must still be exercised even when the
        # planned-condition driver is the caller -- proven indirectly by the
        # fact that a correct run reaches COMPLETE only via real git merges,
        # not by trusting SLICE-A/B's status alone.
        root = self._fixture_copy("planned-dependency-guard")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        from apoapsis.architect.slice_service import package_slice

        with self.assertRaises(Exception):
            package_slice(
                root,
                plan_store,
                slice_store,
                task_store,
                operation_store,
                record.plan_id,
                "SLICE-C",
                expected_plan_version=record.version,
                config=config,
            )

    def test_slice_stopping_at_human_review_halts_the_plan_without_auto_repair(
        self,
    ) -> None:
        root = self._fixture_copy("planned-human-review")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        planner = self._planner(record.plan_id, record.version, record.package_id)

        provider_a = self._provider(
            [action("request_escalation", reason="need a second opinion")]
        )

        with patch(
            "apoapsis.execution.operation_service._build_providers",
            side_effect=[(provider_a, provider_a, None)],
        ):
            result = run_planned_condition(
                root,
                plan_store,
                slice_store,
                task_store,
                operation_store,
                record.plan_id,
                expected_plan_version=record.version,
                config=config,
                planner=planner,
                scenario_id="download-service-v2",
                scenario_version="1.0",
                held_out_oracle=self._oracle(),
            )

        self.assertFalse(result.all_slices_complete)
        self.assertEqual(result.stopped_at_slice_id, "SLICE-A")
        self.assertIsNone(result.held_out_oracle)
        statuses = {item.slice_id: item for item in result.slices}
        self.assertTrue(statuses["SLICE-A"].attempted)
        self.assertFalse(statuses["SLICE-B"].attempted)
        self.assertFalse(statuses["SLICE-C"].attempted)
        self.assertIsNotNone(statuses["SLICE-B"].skip_reason)

    def test_integration_failure_is_detected_when_every_slice_completes_individually(
        self,
    ) -> None:
        root = self._fixture_copy("planned-integration-failure")
        record, plan_store, task_store, slice_store, operation_store, config = (
            self._approve_plan(root)
        )
        planner = self._planner(record.plan_id, record.version, record.package_id)

        provider_a = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/jobs.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/jobs.py"),
                    new_text=_TARGET_JOBS,
                ),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        provider_b = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/downloader.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/downloader.py"),
                    new_text=_BUGGY_DOWNLOADER_DOUBLE_COUNTS_PROGRESS,
                ),
                action("run_check", command_name="v2-downloader-tests"),
                # "v2-jobs-tests" is the one globally required command
                # (ADR 0028); the required-check floor is only satisfied by
                # commands actually executed at the current worktree digest
                # during this session, so every slice must re-run it even
                # when jobs.py is unrelated to its own work -- it already
                # passes here since Slice A's fix is merged in.
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )
        provider_c = self._provider(
            [
                action(
                    "replace_text",
                    path="src/download_service_v2/service.py",
                    old_text=self._read(_FIXTURE, "src/download_service_v2/service.py"),
                    new_text=_TARGET_SERVICE,
                ),
                action("run_check", command_name="v2-service-tests"),
                action("run_check", command_name="v2-jobs-tests"),
            ]
        )

        with patch(
            "apoapsis.execution.operation_service._build_providers",
            side_effect=[
                (provider_a, provider_a, None),
                (provider_b, provider_b, None),
                (provider_c, provider_c, None),
            ],
        ):
            result = run_planned_condition(
                root,
                plan_store,
                slice_store,
                task_store,
                operation_store,
                record.plan_id,
                expected_plan_version=record.version,
                config=config,
                planner=planner,
                scenario_id="download-service-v2",
                scenario_version="1.0",
                held_out_oracle=self._oracle(),
            )

        self.assertTrue(result.all_slices_complete)
        self.assertIsNotNone(result.held_out_oracle)
        self.assertEqual(result.held_out_oracle.status, OracleStatus.FAILED)
        self.assertTrue(result.integration_failure)


def _monolithic_specification_response() -> str:
    import json

    return json.dumps(
        {
            "schema_version": "1.0",
            "task_id": "TASK-PLACEHOLDER",
            "objective": {
                "text": "Add resilient, checksum-verified resumable downloads.",
                "source": "user",
                "source_reference": "cli-request",
            },
            "acceptance_criteria": [
                {
                    "id": "AC-JOBS",
                    "text": "Job records track attempts, progress, checksum, and state.",
                    "source": "derived",
                    "source_reference": "cli-request",
                    "status": "active",
                    "verification_method": "v2-jobs-tests",
                },
                {
                    "id": "AC-DL",
                    "text": "Downloads resume, retry with backoff, and report progress.",
                    "source": "derived",
                    "source_reference": "cli-request",
                    "status": "active",
                    "verification_method": "v2-downloader-tests",
                },
                {
                    "id": "AC-SVC",
                    "text": "The integrated service verifies a checksum before completion.",
                    "source": "derived",
                    "source_reference": "cli-request",
                    "status": "active",
                    "verification_method": "v2-service-tests",
                },
            ],
            "hard_constraints": [
                {
                    "id": "HC-1",
                    "text": "Keep the public API unchanged.",
                    "verbatim_source": "Preserve the current public API.",
                    "interpreted_meaning": "Do not change public signatures.",
                    "source": "user",
                    "source_reference": "cli-request",
                    "scope": "task",
                    "status": "active",
                    "verification_method": "v2-service-tests",
                }
            ],
            "risk_level": "unclassified",
        }
    )


def _inject_task_id(fake: FakeModelProvider) -> None:
    import json

    original_complete = fake.complete

    def complete(invocation):
        output = original_complete(invocation)
        if 'task_id to "' in invocation.prompt:
            task_id = invocation.prompt.split('task_id to "', 1)[1].split('"', 1)[0]
            raw = json.loads(output.content)
            raw["task_id"] = task_id
            return output.model_copy(update={"content": json.dumps(raw)})
        return output

    fake.complete = complete  # type: ignore[method-assign]


class PlanningAggregateTests(unittest.TestCase):
    def _report(
        self,
        *,
        run_id: str,
        monolithic_outcome: TaskOutcome,
        monolithic_oracle: OracleStatus | None,
        planned_all_complete: bool,
        planned_oracle: OracleStatus | None,
    ) -> PlanningComparisonReport:
        from apoapsis.evaluation.schemas import HeldOutOracleResult

        def _report_stub(outcome: TaskOutcome) -> FinalTaskReport:
            return FinalTaskReport(
                task_id="TASK-STUB",
                outcome=outcome,
                number_of_calls=0,
                input_tokens=100,
                output_tokens=50,
                cached_input_tokens=0,
                estimated_cost_usd=0.01,
                latency_seconds=1.0,
                transmitted_files=1,
                transmitted_lines=10,
                agent_turns=2,
                local_agent_turns=2,
                agent_patch_attempts=1,
                agent_verification_runs=1,
            )

        mono_oracle = (
            HeldOutOracleResult(
                oracle_id="o", oracle_version="1.0", source_sha256="0" * 64, status=monolithic_oracle
            )
            if monolithic_oracle is not None
            else None
        )
        planned_oracle_result = (
            HeldOutOracleResult(
                oracle_id="o", oracle_version="1.0", source_sha256="0" * 64, status=planned_oracle
            )
            if planned_oracle is not None
            else None
        )
        planner = PlannerProvenance(
            package_id="PKG-000000000000",
            plan_id="PLAN-000000000000",
            plan_version=1,
            request_package_sha256="0" * 64,
            planner_model="framework-fake-test-planner",
            planner_method=PlannerMethod.FRAMEWORK_FAKE,
            planner_tokens_status=MetricStatus.UNMEASURED,
            reason="test",
        )
        slices = [
            SliceAttemptResult(
                slice_id="SLICE-A",
                attempted=True,
                report=_report_stub(TaskOutcome.COMPLETE if planned_all_complete else TaskOutcome.HUMAN_REVIEW_REQUIRED),
            )
        ]
        return PlanningComparisonReport(
            run_id=run_id,
            scenario_id="download-service-v2",
            scenario_version="1.0",
            task_text="test task",
            coding_model="fake-coder-v1",
            monolithic=MonolithicConditionResult(
                scenario_id="download-service-v2",
                scenario_version="1.0",
                report=_report_stub(monolithic_outcome),
                patch_attempts=1,
                unsafe_patch_rejections=0,
                held_out_oracle=mono_oracle,
            ),
            planned=PlannedConditionResult(
                scenario_id="download-service-v2",
                scenario_version="1.0",
                planner=planner,
                slices=slices,
                all_slices_complete=planned_all_complete,
                held_out_oracle=planned_oracle_result,
                integration_failure=(
                    planned_oracle == OracleStatus.FAILED if planned_all_complete else False
                ),
            ),
        )

    def test_summary_computes_true_completion_and_false_success(self) -> None:
        reports = [
            self._report(
                run_id="RUN-1",
                monolithic_outcome=TaskOutcome.COMPLETE,
                monolithic_oracle=OracleStatus.PASSED,
                planned_all_complete=True,
                planned_oracle=OracleStatus.PASSED,
            ),
            self._report(
                run_id="RUN-2",
                monolithic_outcome=TaskOutcome.COMPLETE,
                monolithic_oracle=OracleStatus.FAILED,
                planned_all_complete=True,
                planned_oracle=OracleStatus.FAILED,
            ),
        ]
        summary = summarize_planning_comparisons(reports, summary_id="SUMMARY-1")
        self.assertEqual(summary.monolithic.true_completion_rate.numerator, 1)
        self.assertEqual(summary.monolithic.true_completion_rate.denominator, 2)
        self.assertEqual(summary.monolithic.false_success_rate.numerator, 1)
        self.assertEqual(summary.monolithic.false_success_rate.denominator, 2)
        self.assertEqual(summary.planned_integration_failure_rate.numerator, 1)
        self.assertEqual(summary.planned_integration_failure_rate.denominator, 2)

    def test_refuses_to_mix_different_scenario_versions(self) -> None:
        report_v1 = self._report(
            run_id="RUN-1",
            monolithic_outcome=TaskOutcome.COMPLETE,
            monolithic_oracle=OracleStatus.PASSED,
            planned_all_complete=True,
            planned_oracle=OracleStatus.PASSED,
        )
        report_v2 = report_v1.model_copy(update={"run_id": "RUN-2", "scenario_version": "2.0"})
        with self.assertRaises(ValueError):
            summarize_planning_comparisons([report_v1, report_v2], summary_id="SUMMARY-X")

    def test_unmeasured_when_no_attempts_reach_the_oracle(self) -> None:
        reports = [
            self._report(
                run_id="RUN-1",
                monolithic_outcome=TaskOutcome.HUMAN_REVIEW_REQUIRED,
                monolithic_oracle=None,
                planned_all_complete=False,
                planned_oracle=None,
            )
        ]
        summary = summarize_planning_comparisons(reports, summary_id="SUMMARY-2")
        self.assertEqual(summary.monolithic.false_success_rate.status, MetricStatus.UNMEASURED)
        self.assertEqual(summary.planned_integration_failure_rate.status, MetricStatus.UNMEASURED)
        self.assertIsNone(summary.monolithic.false_success_rate.value)

    def test_summarize_requires_at_least_one_report(self) -> None:
        with self.assertRaises(ValueError):
            summarize_planning_comparisons([], summary_id="SUMMARY-EMPTY")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apoapsis.config import (
    AgentLoopConfig,
    AgentRoute,
    CompletionPolicy,
    ContextCompilerConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ApoapsisConfig,
)
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.reporting.report import TaskOutcome
from apoapsis.specification.schema import (
    AcceptanceCriterion,
    SourceKind,
    TaskSpecification,
    TraceableStatement,
)
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from apoapsis.workflow.acceptance import (
    AcceptanceCoverageStatus,
    compute_acceptance_coverage,
)
from apoapsis.workflow.engine import SQLiteTaskStore
from apoapsis.workflow.vertical_slice import VerticalSliceRunner
from tests.fakes import FakeModelProvider
from tests.test_vertical_slice import (
    COMPLETE_PATCH,
    IMPLEMENTATION_PATCH,
    REQUEST,
    specification_response,
)

NEW_FILE_PATCH = (
    "diff --git a/src/download_service/new_helper.py "
    "b/src/download_service/new_helper.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/download_service/new_helper.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def helper():\n"
    "+    return True\n"
)

REPLACEMENT_OLD_TEXT = (
    '        mode = "ab" if offset else "wb"\n'
    "        downloaded = offset"
)
REPLACEMENT_NEW_TEXT = (
    "        should_append = offset > 0 and "
    "response.status_code == 206\n"
    '        mode = "ab" if should_append else "wb"\n'
    "        downloaded = offset if should_append else 0"
)


def action(name: str, **values: object) -> str:
    return json.dumps({"action": name, **values})


def specification_with_mapping(
    *, ac1_method: str | None, ac2_method: str | None
) -> str:
    payload = json.loads(specification_response())
    payload["acceptance_criteria"][0]["verification_method"] = ac1_method
    payload["acceptance_criteria"][1]["verification_method"] = ac2_method
    return json.dumps(payload)


def _inject_task_id(fake: FakeModelProvider) -> None:
    original_complete = fake.complete

    def complete(invocation):
        output = original_complete(invocation)
        if len(fake.invocations) == 1:
            task_id = invocation.prompt.split(
                'task_id to "', 1
            )[1].split('"', 1)[0]
            raw = json.loads(output.content)
            raw["task_id"] = task_id
            return output.model_copy(update={"content": json.dumps(raw)})
        return output

    fake.complete = complete  # type: ignore[method-assign]


class AcceptanceCoverageTests(unittest.TestCase):
    """Verification sufficiency and acceptance coverage (ADR 0015).

    Every scenario runs the real `VerticalSliceRunner` / `BoundedAgentSession`
    against the download-service fixture used by `tests/test_agent_loop.py` --
    coverage is never faked at the schema level, it is earned (or not) exactly
    the way a live task would earn it.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "download-service"
        example = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "download-service"
        )
        shutil.copytree(example, self.root)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        self._git("add", ".")
        self._git("commit", "-m", "controlled baseline")
        (self.root / ".apoapsis").mkdir()
        self.store = SQLiteTaskStore(self.root / ".apoapsis" / "apoapsis.db")

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def _config(
        self,
        *,
        completion_policy: CompletionPolicy = CompletionPolicy.STRICT,
        route: AgentRoute = AgentRoute.LOCAL_ONLY,
        frontier_coder: FrontierProviderConfig | None = None,
        local_turns: int = 6,
        frontier_turns: int = 6,
        acceptance: bool = True,
    ) -> ApoapsisConfig:
        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model="fake-coder-v1",
                ),
                frontier_coder=frontier_coder,
            ),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT,
                route=route,
                completion_policy=completion_policy,
                agent=AgentLoopConfig(
                    max_turns=local_turns,
                    max_patch_attempts=3,
                    max_verification_runs=4,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
                frontier_agent=AgentLoopConfig(
                    max_turns=frontier_turns,
                    max_patch_attempts=3,
                    max_verification_runs=4,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
            ),
            context=ContextCompilerConfig(
                max_files=10,
                max_excerpt_lines=200,
                max_total_chars=50_000,
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="download-tests",
                        category="tests",
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        timeout_seconds=30,
                        acceptance=acceptance,
                    )
                ]
            ),
        )

    def _run(
        self, config: ApoapsisConfig, fake: FakeModelProvider, **runner_kwargs
    ):
        _inject_task_id(fake)
        return VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(fake),
            config,
            **runner_kwargs,
        ).run(REQUEST, approve=lambda specification: True)

    # 1. Strict + visible checks pass but no criterion is proven -> the
    # budget exhausts and the task ends in human review, not COMPLETE.
    def test_strict_unmapped_criteria_exhaust_budget_to_human_review(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._config(local_turns=3), fake)

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(report.completion_policy, CompletionPolicy.STRICT)
        self.assertTrue(
            any(
                result.status == "passed"
                for result in report.verification_results
            )
        )
        coverage = {item.criterion_id: item.status for item in report.acceptance_coverage}
        self.assertEqual(
            coverage,
            {
                "AC-1": AcceptanceCoverageStatus.UNPROVEN,
                "AC-2": AcceptanceCoverageStatus.UNPROVEN,
            },
        )

    # 2. Strict + a mapped, acceptance-designated command passes -> COMPLETE
    # with every criterion recorded PROVEN.
    def test_strict_mapped_passing_acceptance_command_reaches_complete(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._config(local_turns=4), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(len(report.acceptance_coverage), 2)
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )
        self.assertTrue(
            all(
                item.evidence_reference == "download-tests"
                for item in report.acceptance_coverage
            )
        )

    # 3. Strict + the mapped command initially fails -> not complete, the
    # agent gets another turn within its ceiling, then it passes -> COMPLETE.
    def test_strict_mapped_command_failing_then_passing_completes(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=REPLACEMENT_OLD_TEXT,
                    new_text=REPLACEMENT_NEW_TEXT,
                ),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._config(local_turns=4), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.agent_verification_runs, 2)
        self.assertEqual(report.verification_results[0].status, "failed")
        self.assertEqual(report.verification_results[1].status, "passed")
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )

    # 4. A criterion mapped to a command the user never designated as an
    # acceptance check stays UNPROVEN even after that command passes -- a
    # model's own mapping proposal has zero authority to make it count.
    def test_mapping_to_non_acceptance_command_has_no_authority(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(
            self._config(local_turns=3, acceptance=False), fake
        )

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertTrue(
            any(
                result.status == "passed"
                for result in report.verification_results
            )
        )
        for item in report.acceptance_coverage:
            self.assertEqual(item.status, AcceptanceCoverageStatus.UNPROVEN)
            self.assertIn("not an approved acceptance check", item.reason)

    # 5. Two different valid tool sequences both reach the same COMPLETE /
    # coverage outcome -- no fixed sequence is enforced.
    def test_two_valid_tool_sequences_reach_the_same_complete_outcome(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        search_then_check = FakeModelProvider(
            [
                spec,
                action("search_repository", query="get_offset"),
                action(
                    "read_file",
                    path="src/download_service/downloader.py",
                    start_line=1,
                    end_line=30,
                ),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        read_then_submit = FakeModelProvider(
            [
                spec,
                action(
                    "read_file",
                    path="src/download_service/downloader.py",
                    start_line=1,
                    end_line=30,
                ),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("submit_for_verification"),
            ]
        )

        report_a = self._run(self._config(local_turns=4), search_then_check)
        report_b = self._run(self._config(local_turns=3), read_then_submit)

        for report in (report_a, report_b):
            self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
            self.assertTrue(
                all(
                    item.status == AcceptanceCoverageStatus.PROVEN
                    for item in report.acceptance_coverage
                )
            )

    # 6. Repeated inspect -> edit -> test -> diagnose -> repair iterations
    # across several turns within configured ceilings, ending proven.
    def test_repeated_inspect_edit_test_diagnose_repair_iterations_end_proven(
        self,
    ) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("search_repository", query="ignore"),
                action(
                    "read_file",
                    path="src/download_service/downloader.py",
                    start_line=1,
                    end_line=30,
                ),
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
                action("inspect_diff"),
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=REPLACEMENT_OLD_TEXT,
                    new_text=REPLACEMENT_NEW_TEXT,
                ),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._config(local_turns=7), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.agent_turns, 7)
        self.assertEqual(report.agent_verification_runs, 2)
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )

    # 7. An unknown run_check command name is rejected without ending the
    # session -- the task continues normally afterward.
    def test_unknown_run_check_command_is_rejected_without_ending_session(
        self,
    ) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("run_check", command_name="does-not-exist"),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._config(local_turns=4), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertGreaterEqual(report.rejected_tool_requests, 1)
        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        first_turn = json.loads(
            (audit / "agent-turn-001.json").read_text(encoding="utf-8")
        )
        self.assertFalse(first_turn["accepted"])
        self.assertIn("unknown verification command", first_turn["summary"])

    # 8. A strict-policy run's prompts/context/evidence never contain
    # "oracle"/"held-out", and `workflow`/`agent` never import the held-out
    # evaluation oracle -- it stays an eval-harness-only side channel.
    def test_strict_policy_never_references_the_held_out_oracle(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for directory_name in ("workflow", "agent"):
            directory = repo_root / "src" / "apoapsis" / directory_name
            for path in directory.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(
                    "oracle",
                    text.lower(),
                    f"{path} must not reference the held-out oracle",
                )

        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._config(local_turns=4), fake)
        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)

        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        for path in audit.rglob("*.json"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("oracle", text)
            self.assertNotIn("held-out", text)
            self.assertNotIn("held_out", text)

    # 9. Baseline policy + unproven coverage still reaches COMPLETE exactly
    # as today, with the policy explicitly recorded on the report.
    def test_baseline_policy_ignores_unproven_coverage_and_completes(self) -> None:
        fake = FakeModelProvider(
            [
                specification_response(),
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(
            self._config(
                completion_policy=CompletionPolicy.BASELINE, local_turns=4
            ),
            fake,
        )

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.completion_policy, CompletionPolicy.BASELINE)
        self.assertEqual(report.acceptance_coverage, [])

    # 10. Strict + local budget exhausts with coverage unproven ->
    # ESCALATION_REQUIRED -> frontier configured -> frontier's own check
    # proves coverage -> COMPLETE. The gate composes with the existing,
    # unmodified escalation machinery.
    def test_strict_local_exhaustion_escalates_and_frontier_proves_coverage(
        self,
    ) -> None:
        frontier_config = FrontierProviderConfig(
            base_url="https://frontier.invalid/v1",
            model="big-coder-v1",
        )
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests"
        )
        local = FakeModelProvider(
            [
                spec,
                action("search_repository", query="ignore"),
                action(
                    "read_file",
                    path="src/download_service/downloader.py",
                    start_line=1,
                    end_line=20,
                ),
            ],
            provider_name="fake_local",
            model_name="small-coder-v1",
        )
        frontier = FakeModelProvider(
            [
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="download-tests"),
            ],
            provider_name="fake_hosted",
            model_name="big-coder-v1",
        )
        _inject_task_id(local)
        report = VerticalSliceRunner(
            self.root,
            self.store,
            InstrumentedModelProvider(local),
            self._config(
                route=AgentRoute.LOCAL_THEN_FRONTIER,
                frontier_coder=frontier_config,
                local_turns=2,
                frontier_turns=4,
            ),
            frontier_coder_provider=InstrumentedModelProvider(frontier),
        ).run(REQUEST, approve=lambda specification: True)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertTrue(report.escalation_triggered)
        self.assertEqual(report.local_agent_turns, 2)
        self.assertEqual(report.frontier_agent_turns, 2)
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )

    def _stale_digest_config(self, *, local_turns: int) -> ApoapsisConfig:
        """Three commands: one required, unmapped "sanity" check that keeps
        the pre-existing dev-verification gate satisfied, plus two
        non-required, acceptance-designated commands, one mapped to each
        acceptance criterion. This isolates acceptance-coverage staleness
        from the unrelated, pre-existing "all required checks passed"
        gate."""

        return ApoapsisConfig(
            models=ModelsConfig(
                frontier=FrontierProviderConfig(
                    base_url="https://provider.invalid/v1",
                    model="fake-coder-v1",
                ),
            ),
            execution=ExecutionConfig(
                mode=ExecutionMode.AGENT,
                route=AgentRoute.LOCAL_ONLY,
                completion_policy=CompletionPolicy.STRICT,
                agent=AgentLoopConfig(
                    max_turns=local_turns,
                    max_patch_attempts=3,
                    max_verification_runs=8,
                    max_search_results=10,
                    max_read_lines=120,
                    max_observation_chars=20_000,
                ),
            ),
            context=ContextCompilerConfig(
                max_files=10, max_excerpt_lines=200, max_total_chars=50_000
            ),
            patch=PatchPolicyConfig(max_changed_lines=100),
            verification=VerificationConfig(
                commands=[
                    VerificationCommand(
                        name="sanity",
                        category="sanity",
                        argv=[sys.executable, "-c", "pass"],
                        timeout_seconds=30,
                        required=True,
                        acceptance=False,
                    ),
                    VerificationCommand(
                        name="download-tests",
                        category="tests",
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        timeout_seconds=30,
                        required=False,
                        acceptance=True,
                    ),
                    VerificationCommand(
                        name="download-tests-again",
                        category="tests",
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        timeout_seconds=30,
                        required=False,
                        acceptance=True,
                    ),
                ]
            ),
        )

    # A digest-bumping edit that does not change behavior, used to move the
    # worktree to a new state without touching the mapped acceptance checks.
    _DIGEST_BUMP_OLD_TEXT = (
        "    def download(self, url: str, destination: Path) -> int:"
    )
    _DIGEST_BUMP_NEW_TEXT = (
        "    def download(self, url: str, destination: Path) -> int:\n"
        "        # digest-bump-noop"
    )

    # A result recorded against an earlier worktree digest must not prove
    # the current one: AC-1's mapped command passes once, the worktree then
    # changes (a new digest), and AC-1 must go back to UNPROVEN until it is
    # re-verified at that new digest -- even though nothing about the fix
    # itself regressed.
    def test_stale_worktree_digest_result_does_not_prove_current_code(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests"),
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=self._DIGEST_BUMP_OLD_TEXT,
                    new_text=self._DIGEST_BUMP_NEW_TEXT,
                ),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests-again"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=6), fake)

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        coverage = {item.criterion_id: item for item in report.acceptance_coverage}
        self.assertEqual(coverage["AC-1"].status, AcceptanceCoverageStatus.UNPROVEN)
        self.assertIn(
            "has not yet been executed", coverage["AC-1"].reason
        )
        self.assertEqual(coverage["AC-2"].status, AcceptanceCoverageStatus.PROVEN)

    # The same script, continued one more turn: re-running the mapped
    # command at the new digest restores proof and reaches COMPLETE.
    def test_reverifying_at_the_new_digest_restores_proof(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests"),
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=self._DIGEST_BUMP_OLD_TEXT,
                    new_text=self._DIGEST_BUMP_NEW_TEXT,
                ),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests-again"),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=7), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )

    # ADR 0017: a brand-new *untracked* file (the common byproduct of a
    # patch that was never `git add`ed) must invalidate earlier verification
    # and coverage exactly like a tracked edit would -- this is the exact
    # gap a `git diff HEAD`-only digest could not see.
    def test_untracked_new_file_creation_invalidates_earlier_proof(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests"),
                action("propose_patch", unified_diff=NEW_FILE_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests-again"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=6), fake)

        self.assertEqual(report.outcome, TaskOutcome.HUMAN_REVIEW_REQUIRED)
        self.assertIn(
            "src/download_service/new_helper.py", report.files_changed
        )
        coverage = {item.criterion_id: item for item in report.acceptance_coverage}
        self.assertEqual(coverage["AC-1"].status, AcceptanceCoverageStatus.UNPROVEN)
        self.assertIn("has not yet been executed", coverage["AC-1"].reason)
        self.assertEqual(coverage["AC-2"].status, AcceptanceCoverageStatus.PROVEN)

    # The same script, continued one more turn: re-running the mapped
    # command against the worktree that now includes the new untracked
    # file restores proof and reaches COMPLETE.
    def test_reverifying_after_untracked_file_creation_restores_proof(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=COMPLETE_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests"),
                action("propose_patch", unified_diff=NEW_FILE_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests-again"),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=7), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )

    # Inspect-diff must show the model the same untracked-file state the
    # verification fingerprint is now sensitive to.
    def test_inspect_diff_exposes_the_new_untracked_file(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=NEW_FILE_PATCH),
                action("inspect_diff"),
                action("run_check", command_name="sanity"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=3), fake)

        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        second_turn = json.loads(
            (audit / "agent-turn-002.json").read_text(encoding="utf-8")
        )
        ledger = second_turn["observation_ledger"]
        diff_entries = [
            item for item in ledger if item["path"] == "<working-tree-diff>"
        ]
        self.assertTrue(diff_entries)
        content = diff_entries[-1]["content"]
        self.assertIn("src/download_service/new_helper.py", content)
        self.assertIn("+def helper():", content)

    # ADR 0018: a failing acceptance-designated command that is not
    # required must still produce real, informative failure evidence and
    # an accurate turn summary -- never "deterministic verification
    # passed" -- even though it correctly does not become a required
    # development gate.
    def test_failing_optional_acceptance_command_gives_evidence_and_accurate_summary(
        self,
    ) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=2), fake)

        self.assertNotEqual(report.outcome, TaskOutcome.COMPLETE)
        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        second_turn = json.loads(
            (audit / "agent-turn-002.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "deterministic verification passed", second_turn["summary"]
        )
        self.assertIn("download-tests", second_turn["summary"])
        self.assertIn("failed", second_turn["summary"])

        failure_path = audit / "verification-failure-001.json"
        self.assertTrue(failure_path.is_file())
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        self.assertEqual(failure["command_name"], "download-tests")
        self.assertIn("AssertionError", failure["relevant_error"])

        ledger = second_turn["observation_ledger"]
        self.assertTrue(any(item["kind"] == "failure" for item in ledger))

    # The model can act on that evidence: edit and re-verify within its
    # existing budgets, reaching a genuine STRICT completion.
    def test_repair_after_seeing_acceptance_failure_evidence_then_completes(
        self,
    ) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests"),
                action(
                    "replace_text",
                    path="src/download_service/downloader.py",
                    old_text=REPLACEMENT_OLD_TEXT,
                    new_text=REPLACEMENT_NEW_TEXT,
                ),
                action("run_check", command_name="sanity"),
                action("run_check", command_name="download-tests"),
                action("run_check", command_name="download-tests-again"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=7), fake)

        self.assertEqual(report.outcome, TaskOutcome.COMPLETE)
        self.assertEqual(report.agent_turns, 7)
        self.assertTrue(
            all(
                item.status == AcceptanceCoverageStatus.PROVEN
                for item in report.acceptance_coverage
            )
        )
        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        third_turn = json.loads(
            (audit / "agent-turn-003.json").read_text(encoding="utf-8")
        )
        self.assertIn("download-tests", third_turn["summary"])
        self.assertIn("failed", third_turn["summary"])

    # An unchanged, identical re-check is still rejected -- but only after
    # the original failing run has already produced its evidence.
    def test_unchanged_duplicate_check_is_rejected_only_after_first_evidence(
        self,
    ) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="download-tests"),
                action("run_check", command_name="download-tests"),
            ]
        )
        report = self._run(self._stale_digest_config(local_turns=3), fake)

        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        second_turn = json.loads(
            (audit / "agent-turn-002.json").read_text(encoding="utf-8")
        )
        third_turn = json.loads(
            (audit / "agent-turn-003.json").read_text(encoding="utf-8")
        )
        self.assertTrue(second_turn["accepted"])
        self.assertIn("failed", second_turn["summary"])
        self.assertFalse(third_turn["accepted"])
        self.assertIn("identical verification already ran", third_turn["summary"])
        self.assertTrue((audit / "verification-failure-001.json").is_file())

    # Ordinary required-command semantics are completely unaffected: a
    # failing required command still produces evidence and an accurate
    # summary exactly as before this change.
    def test_failing_required_command_semantics_are_unaffected(self) -> None:
        spec = specification_with_mapping(
            ac1_method="download-tests", ac2_method="download-tests-again"
        )
        config = self._stale_digest_config(local_turns=2)
        # Make the required "sanity" command itself fail.
        commands = list(config.verification.commands)
        commands[0] = commands[0].model_copy(
            update={"argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}
        )
        config = config.model_copy(
            update={
                "verification": config.verification.model_copy(
                    update={"commands": commands}
                )
            }
        )
        fake = FakeModelProvider(
            [
                spec,
                action("propose_patch", unified_diff=IMPLEMENTATION_PATCH),
                action("run_check", command_name="sanity"),
            ]
        )
        report = self._run(config, fake)

        self.assertNotEqual(report.outcome, TaskOutcome.COMPLETE)
        audit = self.root / ".apoapsis" / "tasks" / report.task_id
        second_turn = json.loads(
            (audit / "agent-turn-002.json").read_text(encoding="utf-8")
        )
        self.assertIn("sanity", second_turn["summary"])
        self.assertIn("failed", second_turn["summary"])
        self.assertTrue((audit / "verification-failure-001.json").is_file())
        failure = json.loads(
            (audit / "verification-failure-001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(failure["command_name"], "sanity")


class ComputeAcceptanceCoverageUnitTests(unittest.TestCase):
    """Direct, fast unit coverage of the tri-state execution semantics
    (ADR 0016): never executed, executed-and-failed, and executed-and-passed
    must never collapse into one another."""

    def setUp(self) -> None:
        self.specification = TaskSpecification(
            task_id="TASK-COVERAGE-UNIT",
            objective=TraceableStatement(
                text="Add resumable downloads.",
                source=SourceKind.USER,
                source_reference="unit-test",
            ),
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-1",
                    text="Interrupted downloads resume from the persisted byte.",
                    source=SourceKind.DERIVED,
                    source_reference="unit-test",
                    verification_method="acceptance-check",
                )
            ],
        )
        self.commands = [
            VerificationCommand(
                name="acceptance-check",
                category="tests",
                argv=["true"],
                acceptance=True,
            )
        ]

    def _status(self, command_results: dict[str, VerificationStatus]):
        coverage = compute_acceptance_coverage(
            self.specification, self.commands, command_results
        )
        self.assertEqual(len(coverage), 1)
        return coverage[0]

    def test_never_executed_is_unproven_not_failed(self) -> None:
        result = self._status({})
        self.assertEqual(result.status, AcceptanceCoverageStatus.UNPROVEN)
        self.assertIn("has not yet been executed", result.reason)

    def test_executed_and_failed_is_failed(self) -> None:
        result = self._status({"acceptance-check": VerificationStatus.FAILED})
        self.assertEqual(result.status, AcceptanceCoverageStatus.FAILED)

    def test_executed_and_timed_out_is_failed(self) -> None:
        result = self._status({"acceptance-check": VerificationStatus.TIMED_OUT})
        self.assertEqual(result.status, AcceptanceCoverageStatus.FAILED)

    def test_executed_and_errored_is_failed(self) -> None:
        result = self._status({"acceptance-check": VerificationStatus.ERROR})
        self.assertEqual(result.status, AcceptanceCoverageStatus.FAILED)

    def test_executed_and_passed_is_proven(self) -> None:
        result = self._status({"acceptance-check": VerificationStatus.PASSED})
        self.assertEqual(result.status, AcceptanceCoverageStatus.PROVEN)

    def test_skipped_is_treated_as_never_executed(self) -> None:
        # A caller must omit SKIPPED entries entirely (they were never
        # actually executed), but if one leaks through it must still not
        # count as proof either way.
        result = self._status({"acceptance-check": VerificationStatus.SKIPPED})
        self.assertEqual(result.status, AcceptanceCoverageStatus.UNPROVEN)


if __name__ == "__main__":
    unittest.main()

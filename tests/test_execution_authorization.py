from __future__ import annotations

from unittest.mock import patch

from apoapsis.config import (
    AgentLoopConfig,
    CompletionPolicy,
    ContextCompilerConfig,
    FrontierProviderConfig,
    ModelsConfig,
    PatchPolicyConfig,
    ApoapsisConfig,
)
from apoapsis.execution.authorization import build_execution_authorization_package
from apoapsis.execution.backend import (
    DockerBackendConfig,
    ExecutionBackendConfig,
    ExecutionBackendName,
)
from apoapsis.execution.operation_errors import ExecutionAuthorizationDriftError
from apoapsis.execution.operation_schema import ExecutionOperationStatus
from apoapsis.execution.operation_service import (
    prepare_execution_operation,
    run_execution_operation,
)
from apoapsis.repository.readiness import DirtyParentRepositoryError
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from tests.test_execution_operations import ExecutionOperationTestsBase


class ExecutionAuthorizationPackageTests(ExecutionOperationTestsBase):
    """Direct tests of ``build_execution_authorization_package`` itself,
    independent of the operation store/service plumbing."""

    def test_package_hash_is_stable_across_repeated_builds(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        task = self.store.get_task(task_id)
        first = build_execution_authorization_package(
            self.root,
            operation_id="EXOP-STABLE-1",
            task_id=task_id,
            task_version=version,
            specification=task.specification,
            config=config,
        )
        second = build_execution_authorization_package(
            self.root,
            operation_id="EXOP-STABLE-1",
            task_id=task_id,
            task_version=version,
            specification=task.specification,
            config=config,
        )
        self.assertEqual(first.package_sha256, second.package_sha256)

    def test_package_hash_is_independent_of_operation_id(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        task = self.store.get_task(task_id)
        preview = build_execution_authorization_package(
            self.root,
            operation_id="EXOP-PREVIEW",
            task_id=task_id,
            task_version=version,
            specification=task.specification,
            config=config,
        )
        real = build_execution_authorization_package(
            self.root,
            operation_id="EXOP-REALSUBMIT01",
            task_id=task_id,
            task_version=version,
            specification=task.specification,
            config=config,
        )
        self.assertEqual(preview.package_sha256, real.package_sha256)

    def test_package_never_contains_verification_environment_values(self) -> None:
        task_id, version = self._create_approved_task()
        config = self._one_shot_config()
        config = config.model_copy(
            update={
                "verification": VerificationConfig(
                    commands=[
                        VerificationCommand(
                            name="secret-check",
                            category="tests",
                            argv=["true"],
                            environment={"API_TOKEN": "super-secret-value"},
                        )
                    ]
                )
            }
        )
        task = self.store.get_task(task_id)
        package = build_execution_authorization_package(
            self.root,
            operation_id="EXOP-SECRET-1",
            task_id=task_id,
            task_version=version,
            specification=task.specification,
            config=config,
        )
        dumped = package.model_dump_json()
        self.assertNotIn("super-secret-value", dumped)


class ExecutionAuthorizationDriftTests(ExecutionOperationTestsBase):
    """ADR 0026: ``run_execution_operation`` must reject -- before any
    provider construction, worktree mutation, or command execution -- if
    the task, specification, repository state, or execution configuration
    changed since the operation was authorized at
    ``prepare_execution_operation`` time."""

    def _prepare(self, operation_id: str, config: ApoapsisConfig) -> tuple[str, int]:
        task_id, version = self._create_approved_task()
        prepare_execution_operation(
            self.root,
            self.store,
            self.operation_store,
            task_id=task_id,
            operation_id=operation_id,
            expected_version=version,
            config=config,
        )
        return task_id, version

    def _assert_rejected_before_provider_construction(
        self, operation_id: str, config: ApoapsisConfig, *, exception: type[Exception]
    ) -> None:
        with patch(
            "apoapsis.execution.operation_service._build_providers"
        ) as build_providers:
            with self.assertRaises(exception):
                run_execution_operation(
                    self.root,
                    self.store,
                    self.operation_store,
                    config,
                    operation_id=operation_id,
                )
            build_providers.assert_not_called()
        self.assertEqual(
            self.operation_store.get(operation_id).status,
            ExecutionOperationStatus.FAILED,
        )

    def test_tracked_edit_after_prepare_is_rejected(self) -> None:
        config = self._one_shot_config()
        self._prepare("EXOP-DRIFT-TRACKED", config)
        (self.root / "README.md").write_text(
            "edited after authorization, never committed\n", encoding="utf-8"
        )
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-TRACKED", config, exception=DirtyParentRepositoryError
        )

    def test_untracked_edit_after_prepare_is_rejected(self) -> None:
        config = self._one_shot_config()
        self._prepare("EXOP-DRIFT-UNTRACKED", config)
        (self.root / "new-untracked-file.txt").write_text("surprise\n", encoding="utf-8")
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-UNTRACKED", config, exception=DirtyParentRepositoryError
        )

    def test_model_change_after_prepare_is_rejected(self) -> None:
        config = self._one_shot_config()
        self._prepare("EXOP-DRIFT-MODEL", config)
        changed = config.model_copy(
            update={
                "models": ModelsConfig(
                    frontier=FrontierProviderConfig(
                        base_url="https://provider.invalid/v1",
                        model="a-different-model",
                    )
                )
            }
        )
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-MODEL", changed, exception=ExecutionAuthorizationDriftError
        )

    def test_budget_change_after_prepare_is_rejected(self) -> None:
        config = self._agent_config(local_turns=3)
        self._prepare("EXOP-DRIFT-BUDGET", config)
        changed = config.model_copy(
            update={
                "execution": config.execution.model_copy(
                    update={
                        "agent": AgentLoopConfig(
                            max_turns=30,
                            max_patch_attempts=2,
                            max_verification_runs=2,
                            max_search_results=10,
                            max_read_lines=120,
                            max_observation_chars=20_000,
                        )
                    }
                )
            }
        )
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-BUDGET", changed, exception=ExecutionAuthorizationDriftError
        )

    def test_verification_command_change_after_prepare_is_rejected(self) -> None:
        config = self._one_shot_config()
        self._prepare("EXOP-DRIFT-VERIFY", config)
        changed = config.model_copy(
            update={
                "verification": VerificationConfig(
                    commands=[
                        VerificationCommand(
                            name="download-tests",
                            category="tests",
                            argv=["true", "--a-different-argv"],
                            timeout_seconds=30,
                        )
                    ]
                )
            }
        )
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-VERIFY", changed, exception=ExecutionAuthorizationDriftError
        )

    def test_completion_policy_change_after_prepare_is_rejected(self) -> None:
        config = self._one_shot_config()
        self._prepare("EXOP-DRIFT-POLICY", config)
        changed = config.model_copy(
            update={
                "execution": config.execution.model_copy(
                    update={"completion_policy": CompletionPolicy.STRICT}
                )
            }
        )
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-POLICY", changed, exception=ExecutionAuthorizationDriftError
        )

    def test_backend_change_after_prepare_is_rejected(self) -> None:
        config = self._one_shot_config()
        self._prepare("EXOP-DRIFT-BACKEND", config)
        docker_backend = ExecutionBackendConfig(
            backend=ExecutionBackendName.DOCKER,
            docker=DockerBackendConfig(
                image="apoapsis-sandbox:latest",
                image_digest="sha256:" + "0" * 64,
            ),
        )
        changed = config.model_copy(
            update={
                "verification": config.verification.model_copy(
                    update={"backend": docker_backend}
                )
            }
        )
        self._assert_rejected_before_provider_construction(
            "EXOP-DRIFT-BACKEND", changed, exception=ExecutionAuthorizationDriftError
        )

    def test_unchanged_configuration_is_not_rejected(self) -> None:
        """Negative control: an unmodified re-authorization must not be
        flagged as drift -- proves the mechanism detects real changes, not
        merely being recomputed."""

        config = self._one_shot_config()
        task_id, version = self._prepare("EXOP-DRIFT-NONE", config)
        with patch(
            "apoapsis.execution.operation_service._build_providers",
            side_effect=RuntimeError("stop before any real work"),
        ):
            with self.assertRaises(RuntimeError):
                run_execution_operation(
                    self.root,
                    self.store,
                    self.operation_store,
                    config,
                    operation_id="EXOP-DRIFT-NONE",
                )
        # Reached provider construction (and failed there, as instructed)
        # rather than being rejected earlier for spurious drift.
        record = self.operation_store.get("EXOP-DRIFT-NONE")
        self.assertIn("stop before any real work", record.error or "")

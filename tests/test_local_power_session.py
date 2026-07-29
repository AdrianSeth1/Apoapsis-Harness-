"""Deterministic safety-boundary coverage for the Local Power Sandbox (ADR 0059).

Every test here drives a *fake* provider through a scripted sequence of
actions. Nothing calls a real model, and nothing depends on wall-clock
scheduling or network availability, so a failure in this file always means the
boundary itself moved rather than that an environment was slow or offline.

The organizing question for the whole file is deliberately narrow: given a
model that asks for something it should not get, does the harness refuse,
audit the refusal, and keep completion authority for itself?
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apoapsis.agent.power_actions import (
    PowerActionError,
    parse_power_action,
    power_action_schema,
)
from apoapsis.agent.power_session import LocalPowerSession
from apoapsis.agent.sandbox import (
    SandboxGuard,
    SandboxShell,
    SandboxViolation,
    ShellPolicy,
)
from apoapsis.agent.session import AgentSessionOutcome
from apoapsis.audit.store import TaskAuditStore
from apoapsis.config import (
    ApoapsisConfig,
    ExecutionConfig,
    ExecutionMode,
    FrontierProviderConfig,
    LocalPowerConfig,
    ModelsConfig,
)
from apoapsis.context.compiler import ContextPackage
from apoapsis.review.case import (
    read_agent_session,
    read_local_power_review_package,
    read_local_power_session,
    read_local_stage_session,
)
from apoapsis.verification.results import VerificationStatus
from apoapsis.verification.runner import VerificationCommand, VerificationConfig
from tests.helpers import make_specification


def power_action(name: str, **values: object) -> str:
    return json.dumps({"action": name, **values})


class ScriptedModel:
    """A fake coding model that emits one pre-written action per turn.

    Raises rather than looping if the session asks for more turns than the
    script supplies, so a session that silently keeps going instead of
    stopping at its boundary fails loudly here instead of hanging.
    """

    def __init__(self, actions: list[str]) -> None:
        self.actions = list(actions)
        self.prompts: list[str] = []

    def __call__(self, operation, prompt, context, **kwargs):
        self.prompts.append(prompt)
        if not self.actions:
            raise AssertionError(
                "scripted model exhausted: the session took more turns than expected"
            )
        return SimpleNamespace(content=self.actions.pop(0))


class LocalPowerTestBase(unittest.TestCase):
    """A real, disposable Git worktree standing in for the sandbox."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="apoapsis-local-power-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Apoapsis Tests")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        # The exact things the boundary must protect, present as real files so
        # a containment failure would be observable as real content leaking
        # rather than as a missing-file error that masks the bug.
        (self.root / ".apoapsis").mkdir(exist_ok=True)
        (self.root / ".apoapsis" / "config.toml").write_text(
            "harness = 'do not touch'\n", encoding="utf-8"
        )
        (self.root / ".env").write_text("API_TOKEN=super-secret\n", encoding="utf-8")
        self._git("add", "src")
        self._git("commit", "-m", "baseline")
        self.specification = make_specification()
        self.context = ContextPackage.specification_only(self.specification, "0" * 40)
        self.audit = TaskAuditStore(self.root, self.specification.task_id)

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True
        )

    def make_config(self, **overrides: object) -> LocalPowerConfig:
        base: dict[str, object] = {
            "enabled": True,
            "max_turns": 6,
            "max_seconds": 300.0,
            "max_shell_commands": 5,
        }
        base.update(overrides)
        return LocalPowerConfig(**base)

    def make_session(
        self,
        actions: list[str],
        *,
        config: LocalPowerConfig | None = None,
        verification: VerificationConfig | None = None,
        shell: SandboxShell | None = None,
    ) -> tuple[LocalPowerSession, ScriptedModel]:
        model = ScriptedModel(actions)
        session = LocalPowerSession(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=config or self.make_config(),
            verification_config=verification or VerificationConfig(commands=[]),
            audit=self.audit,
            model_call=model,
            shell=shell,
        )
        return session, model

    def passing_verification(self) -> VerificationConfig:
        return VerificationConfig(
            commands=[
                VerificationCommand(
                    name="unit",
                    category="test",
                    argv=[sys.executable, "-c", "raise SystemExit(0)"],
                    timeout_seconds=60,
                )
            ]
        )

    def failing_verification(self) -> VerificationConfig:
        return VerificationConfig(
            commands=[
                VerificationCommand(
                    name="unit",
                    category="test",
                    argv=[sys.executable, "-c", "raise SystemExit(1)"],
                    timeout_seconds=60,
                )
            ]
        )

    def two_required_commands(self) -> VerificationConfig:
        """Two required passing checks, so that one of them passing is not
        by itself sufficient and the session keeps running."""

        return VerificationConfig(
            commands=[
                VerificationCommand(
                    name="unit",
                    category="test",
                    argv=[sys.executable, "-c", "raise SystemExit(0)"],
                    timeout_seconds=60,
                ),
                VerificationCommand(
                    name="lint",
                    category="lint",
                    argv=[sys.executable, "-c", "raise SystemExit(0)"],
                    timeout_seconds=60,
                ),
            ]
        )


class OptInTests(LocalPowerTestBase):
    """Requirement 1: the mode is opt-in and disabled by default."""

    def test_local_power_is_disabled_by_default(self) -> None:
        self.assertFalse(LocalPowerConfig().enabled)
        self.assertFalse(ExecutionConfig().local_power.enabled)

    def test_default_config_keeps_shell_and_network_conservative(self) -> None:
        config = LocalPowerConfig()
        self.assertFalse(config.allow_network)
        self.assertEqual(config.max_turns, 8)
        self.assertTrue(config.require_verification)
        self.assertTrue(config.require_final_diff_review)

    def test_forbidden_boundary_entries_cannot_be_dropped(self) -> None:
        """A local override may widen the list but never remove the floor."""

        with self.assertRaises(ValueError) as caught:
            LocalPowerConfig(forbidden_paths=["*.pem"])
        self.assertIn(".apoapsis/**", str(caught.exception))

    def test_enabling_requires_bounded_agent_execution_mode(self) -> None:
        models = ModelsConfig(
            frontier=FrontierProviderConfig(
                base_url="https://api.example.invalid", model="frontier-1"
            )
        )
        with self.assertRaises(ValueError) as caught:
            ApoapsisConfig(
                models=models,
                verification=VerificationConfig(commands=[]),
                execution=ExecutionConfig(
                    mode=ExecutionMode.ONE_SHOT,
                    local_power=LocalPowerConfig(enabled=True),
                ),
            )
        self.assertIn("requires execution mode 'agent'", str(caught.exception))


class FileActionTests(LocalPowerTestBase):
    """Requirements 2-6: what the model may and may not touch."""

    def test_model_can_write_a_new_file_inside_the_sandbox(self) -> None:
        session, _ = self.make_session(
            [
                power_action(
                    "write_file", path="src/config.py", content="SETTING = True\n"
                ),
                power_action("finish", summary="added a config module"),
            ]
        )
        result = session.run()
        created = self.root / "src" / "config.py"
        self.assertTrue(created.is_file())
        self.assertEqual(created.read_text(encoding="utf-8"), "SETTING = True\n")
        self.assertIn("src/config.py", result.changed_files)
        self.assertEqual(session.rejections, [])

    def test_model_cannot_write_apoapsis_config(self) -> None:
        original = (self.root / ".apoapsis" / "config.toml").read_text(encoding="utf-8")
        session, _ = self.make_session(
            [
                power_action(
                    "write_file", path=".apoapsis/config.toml", content="owned = true\n"
                ),
                power_action("finish", summary="tried to reconfigure the harness"),
            ]
        )
        session.run()
        self.assertEqual(
            (self.root / ".apoapsis" / "config.toml").read_text(encoding="utf-8"),
            original,
        )
        self.assertEqual(len(session.rejections), 1)
        self.assertIn(".apoapsis", session.rejections[0].reason)

    def test_model_cannot_write_git_config(self) -> None:
        session, _ = self.make_session(
            [
                power_action(
                    "write_file",
                    path=".git/config",
                    content="[remote]\n\turl = https://example.invalid\n",
                ),
                power_action("finish", summary="tried to rewrite git config"),
            ]
        )
        session.run()
        self.assertNotIn(
            "url = https://example.invalid",
            (self.root / ".git" / "config").read_text(encoding="utf-8"),
        )
        self.assertEqual(len(session.rejections), 1)
        self.assertIn(".git", session.rejections[0].reason)

    def test_model_cannot_escape_the_sandbox_with_parent_traversal(self) -> None:
        outside = self.root.parent / "escaped-marker.txt"
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        session, _ = self.make_session(
            [
                power_action(
                    "write_file", path="../escaped-marker.txt", content="escaped\n"
                ),
                power_action(
                    "write_file",
                    path="src/../../escaped-marker.txt",
                    content="escaped\n",
                ),
                power_action("finish", summary="tried to leave the sandbox"),
            ]
        )
        session.run()
        self.assertFalse(outside.exists())
        self.assertEqual(len(session.rejections), 2)
        for record in session.rejections:
            self.assertIn("traverse outside the sandbox", record.reason)

    def test_model_cannot_write_an_absolute_path(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="/tmp/escaped.txt", content="x\n"),
                power_action("finish", summary="tried an absolute path"),
            ]
        )
        session.run()
        self.assertEqual(len(session.rejections), 1)
        self.assertIn("sandbox-relative", session.rejections[0].reason)

    def test_model_cannot_read_forbidden_files(self) -> None:
        """Reading a secret is the harm; read is refused exactly like write."""

        session, _ = self.make_session(
            [
                power_action("read_file", path=".env"),
                power_action("read_file", path=".apoapsis/config.toml"),
                power_action("finish", summary="tried to read secrets"),
            ]
        )
        session.run()
        self.assertEqual(len(session.rejections), 2)
        transcript = json.dumps([item.model_dump(mode="json") for item in session.records])
        self.assertNotIn("super-secret", transcript)
        observations = json.dumps(
            [item.model_dump(mode="json") for item in session.observations]
        )
        self.assertNotIn("super-secret", observations)

    def test_model_cannot_delete_forbidden_files(self) -> None:
        session, _ = self.make_session(
            [
                power_action("delete_file", path=".env"),
                power_action("finish", summary="tried to delete the env file"),
            ]
        )
        session.run()
        self.assertTrue((self.root / ".env").is_file())
        self.assertEqual(len(session.rejections), 1)

    def test_a_symlink_pointing_outside_the_sandbox_is_refused(self) -> None:
        outside_dir = Path(tempfile.mkdtemp(prefix="apoapsis-outside-"))
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        secret = outside_dir / "secret.txt"
        secret.write_text("outside-content\n", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("this platform does not permit creating symlinks")
        guard = SandboxGuard(
            self.root, forbidden_paths=LocalPowerConfig().forbidden_paths
        )
        with self.assertRaises(SandboxViolation):
            guard.resolve("link.txt")


class ShellMediationTests(LocalPowerTestBase):
    """Requirements 7-9: how mediated shell behaves."""

    def _recording_shell(self, *, completed=None, raises=None) -> tuple[SandboxShell, dict]:
        seen: dict = {}

        def runner(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            if raises is not None:
                raise raises
            return completed or subprocess.CompletedProcess(argv, 0, "ok\n", "")

        guard = SandboxGuard(
            self.root, forbidden_paths=LocalPowerConfig().forbidden_paths
        )
        shell = SandboxShell(
            self.root,
            policy=ShellPolicy(
                allow_shell=True,
                allow_network=False,
                timeout_seconds=30.0,
                max_output_chars=10_000,
            ),
            guard=guard,
            runner=runner,
            environ={
                "PATH": os.environ.get("PATH", ""),
                "OPENAI_API_KEY": "leaked-key",
                "AWS_SECRET_ACCESS_KEY": "leaked-secret",
                "HOME": "/home/operator",
            },
        )
        return shell, seen

    def test_shell_command_runs_with_the_sandbox_as_working_directory(self) -> None:
        shell, seen = self._recording_shell()
        session, _ = self.make_session(
            [
                power_action("run_shell", command="python -m compileall src"),
                power_action("finish", summary="compiled the sources"),
            ],
            shell=shell,
        )
        session.run()
        self.assertEqual(seen["cwd"], str(self.root))
        self.assertFalse(seen["shell"])
        self.assertEqual(len(session.commands_run), 1)
        self.assertEqual(session.commands_run[0].cwd, str(self.root))

    def test_shell_environment_is_scrubbed_of_secrets(self) -> None:
        shell, seen = self._recording_shell()
        session, _ = self.make_session(
            [
                power_action("run_shell", command="python -m compileall src"),
                power_action("finish", summary="done"),
            ],
            shell=shell,
        )
        session.run()
        environment = seen["env"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertNotIn("leaked-key", json.dumps(environment))
        self.assertEqual(environment["APOAPSIS_LOCAL_POWER_SANDBOX"], "1")

    def test_shell_command_timeout_is_enforced_and_recorded(self) -> None:
        shell, _ = self._recording_shell(
            raises=subprocess.TimeoutExpired(cmd=["python"], timeout=30.0)
        )
        session, _ = self.make_session(
            [
                power_action("run_shell", command="python -m unittest discover"),
                power_action("finish", summary="ran the tests"),
            ],
            shell=shell,
        )
        session.run()
        self.assertEqual(len(session.commands_run), 1)
        self.assertTrue(session.commands_run[0].timed_out)
        self.assertIsNone(session.commands_run[0].exit_code)

    def test_timeout_passed_to_the_runner_respects_the_policy(self) -> None:
        shell, seen = self._recording_shell()
        session, _ = self.make_session(
            [
                power_action("run_shell", command="python -m compileall src"),
                power_action("finish", summary="done"),
            ],
            shell=shell,
        )
        session.run()
        self.assertLessEqual(seen["timeout"], 30.0)

    def test_forbidden_shell_command_is_rejected_and_audited(self) -> None:
        shell, seen = self._recording_shell()
        session, _ = self.make_session(
            [
                power_action("run_shell", command="git reset --hard HEAD"),
                power_action("run_shell", command="curl https://example.invalid"),
                power_action(
                    "run_shell", command="python -m unittest discover -s ../../etc"
                ),
                power_action("finish", summary="tried some commands"),
            ],
            shell=shell,
        )
        session.run()
        self.assertNotIn("argv", seen, "a refused command must never be executed")
        self.assertEqual(len(session.rejections), 3)
        self.assertEqual(session.commands_run, [])
        # Refusals are durable, not just in-memory.
        rejection_files = sorted(
            path.name
            for path in (self.root / ".apoapsis" / "tasks" / self.specification.task_id).glob(
                "local-power-rejection-*.json"
            )
        )
        self.assertEqual(len(rejection_files), 3)

    def test_a_refused_command_does_not_consume_the_shell_budget(self) -> None:
        shell, _ = self._recording_shell()
        session, _ = self.make_session(
            [
                power_action("run_shell", command="git push origin main"),
                power_action("finish", summary="done"),
            ],
            shell=shell,
        )
        session.run()
        self.assertEqual(session.budget.shell_commands_used, 0)

    def test_network_commands_are_refused_when_network_is_disabled(self) -> None:
        shell, _ = self._recording_shell()
        with self.assertRaises(SandboxViolation) as caught:
            shell.parse("pip install requests")
        self.assertIn("network", str(caught.exception))

    def test_shell_metacharacters_are_refused_outright(self) -> None:
        shell, _ = self._recording_shell()
        for command in (
            "python -m compileall src; cat .env",
            "python -m compileall src && rm -rf /",
            "python -m compileall $(whoami)",
        ):
            with self.assertRaises(SandboxViolation):
                shell.parse(command)

    def test_shell_is_refused_entirely_when_allow_shell_is_false(self) -> None:
        session, _ = self.make_session(
            [
                power_action("run_shell", command="python -m compileall src"),
                power_action("finish", summary="tried to run a command"),
            ],
            config=self.make_config(allow_shell=False, max_shell_commands=0),
        )
        session.run()
        self.assertEqual(len(session.rejections), 1)
        self.assertIn("disabled", session.rejections[0].reason)


class DiffAndVerificationAuthorityTests(LocalPowerTestBase):
    """Requirements 10-14: who computes the diff and who decides the outcome."""

    def test_final_diff_is_computed_by_the_harness(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 42\n"),
                power_action("finish", summary="bumped the value"),
            ]
        )
        session.run()
        package = session.review_package
        self.assertIsNotNone(package)
        # The model never emitted diff syntax; the harness derived all of it.
        self.assertIn("diff --git", package.final_diff)
        self.assertIn("-VALUE = 1", package.final_diff)
        self.assertIn("+VALUE = 42", package.final_diff)
        self.assertIn("src/app.py", package.changed_files)

    def test_finish_alone_does_not_mark_the_task_complete(self) -> None:
        """The model claims success; verification is what actually decides."""

        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 2\n"),
                power_action("finish", summary="all done, everything passes"),
            ],
            verification=self.failing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)
        self.assertFalse(session.review_package.verification_passed)
        self.assertEqual(
            session.review_package.model_summary, "all done, everything passes"
        )

    def test_finish_without_any_configured_verification_is_not_complete(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 3\n"),
                power_action("finish", summary="done"),
            ],
            verification=VerificationConfig(commands=[]),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)

    def test_failed_verification_produces_a_human_review_package(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 4\n"),
                power_action("finish", summary="done"),
            ],
            verification=self.failing_verification(),
        )
        result = session.run()
        package = session.review_package
        self.assertTrue(package.requires_human_review)
        self.assertFalse(package.verification_passed)
        self.assertIn("verification did not pass", result.stop_reason)
        self.assertTrue(package.final_diff)

    def test_passed_verification_produces_a_normal_report_and_diff_package(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 5\n"),
                power_action("finish", summary="done"),
            ],
            verification=self.passing_verification(),
        )
        result = session.run()
        package = session.review_package
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        self.assertTrue(package.verification_passed)
        self.assertFalse(package.requires_human_review)
        self.assertIn("src/app.py", package.changed_files)
        self.assertGreaterEqual(len(package.verification_results), 1)

    def test_verification_still_runs_when_the_model_never_calls_finish(self) -> None:
        """Turn exhaustion is not a bypass: the harness verifies anyway."""

        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 6\n"),
                power_action("read_file", path="src/app.py"),
            ],
            config=self.make_config(max_turns=2),
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        self.assertGreaterEqual(result.verification_runs, 1)

    def test_a_session_that_changed_nothing_is_never_complete(self) -> None:
        session, _ = self.make_session(
            [
                power_action("read_file", path="src/app.py"),
                power_action("finish", summary="nothing needed changing"),
            ],
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)
        self.assertEqual(result.changed_files, [])


class DeterministicTerminationTests(LocalPowerTestBase):
    """ADR 0069: a passing configured contract ends the session by itself.

    The defect these cover was observed live on TASK-33E0EB6476C4: the first
    verification passed on turn 4, and because nothing in the loop treated
    that as terminal, the model was asked for four more actions and spent
    every one of them re-requesting the identical check. `ScriptedModel`
    raises when it is asked for an action the test did not write, so a
    session that fails to stop fails here loudly rather than quietly
    burning budget.
    """

    def test_passing_verification_ends_the_session_immediately(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 7\n"),
                power_action("run_verification", command_name="unit"),
            ],
            config=self.make_config(max_turns=8),
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        # Two turns used out of eight, and the script was consumed exactly.
        self.assertEqual(len(result.turn_records), 2)
        self.assertEqual(model.actions, [])
        self.assertIn(
            "configured verification passed for the current sandbox state",
            result.stop_reason,
        )

    def test_the_passing_result_is_reused_instead_of_run_a_second_time(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 8\n"),
                power_action("run_verification", command_name="unit"),
            ],
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        # One run, not two: finalization reused the result recorded for this
        # exact worktree fingerprint rather than re-deriving it.
        self.assertEqual(result.verification_runs, 1)

    def test_partial_verification_does_not_end_the_session(self) -> None:
        """One passing command out of two required ones proves half a
        contract, and half a contract is not a reason to stop."""

        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 9\n"),
                power_action("run_verification", command_name="unit"),
                power_action("read_file", path="src/app.py"),
                # `lint` is still required and still unrun, so this finish is
                # refused by the ADR 0070 gate; running it satisfies the
                # contract and the harness ends the session itself.
                power_action("finish", summary="done"),
                power_action("run_verification", command_name="lint"),
            ],
            verification=self.two_required_commands(),
        )
        result = session.run()
        self.assertEqual(len(result.turn_records), 5)
        self.assertEqual(model.actions, [])
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)

    def test_an_edit_invalidates_an_earlier_passing_result(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 10\n"),
                power_action("run_verification", command_name="unit"),
            ],
            verification=self.passing_verification(),
        )
        session.run()
        digest_before = session._verification_state_digest()
        self.assertIn(digest_before, session.command_results)
        # The same command, against changed code, is a different question.
        (self.root / "src" / "app.py").write_text("VALUE = 11\n", encoding="utf-8")
        self.assertNotEqual(session._verification_state_digest(), digest_before)
        self.assertFalse(session._verification_sufficient())

    def test_repeating_an_identical_verification_is_refused_not_rerun(self) -> None:
        """The exact live failure loop, forced: the second identical request
        must not execute and must not be accepted."""

        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 12\n"),
                power_action("run_verification", command_name="unit"),
                power_action("run_verification", command_name="unit"),
                power_action("run_verification", command_name="lint"),
            ],
            verification=self.two_required_commands(),
        )
        result = session.run()
        repeated = [
            item
            for item in result.turn_records
            if item.action == "run_verification" and not item.accepted
        ]
        self.assertEqual(len(repeated), 1)
        self.assertIn("already ran for the current sandbox state", repeated[0].summary)
        # Refused, therefore never executed: only the first request ran.
        self.assertEqual(len(session.verification_results), 2)
        self.assertEqual(
            [item.action for item in session.rejections], ["run_verification"]
        )

    def test_failed_verification_still_leaves_room_for_a_repair_turn(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 13\n"),
                power_action("run_verification", command_name="unit"),
                power_action("write_file", path="src/app.py", content="VALUE = 14\n"),
                power_action("finish", summary="repaired"),
            ],
            verification=self.failing_verification(),
        )
        result = session.run()
        self.assertEqual(len(result.turn_records), 4)
        self.assertEqual(model.actions, [])
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)

    def test_a_contract_with_nothing_required_never_terminates_early(self) -> None:
        verification = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="unit",
                    category="test",
                    argv=[sys.executable, "-c", "raise SystemExit(0)"],
                    timeout_seconds=60,
                    required=False,
                )
            ]
        )
        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 15\n"),
                power_action("run_verification", command_name="unit"),
                power_action("finish", summary="done"),
            ],
            verification=verification,
        )
        result = session.run()
        self.assertEqual(len(result.turn_records), 3)
        self.assertEqual(model.actions, [])

    def test_a_session_that_changed_nothing_never_terminates_early(self) -> None:
        session, model = self.make_session(
            [
                power_action("run_verification", command_name="unit"),
                power_action("finish", summary="nothing to do"),
            ],
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(len(result.turn_records), 2)
        self.assertEqual(model.actions, [])
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)

    def test_finish_before_any_verification_still_triggers_harness_verification(
        self,
    ) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 16\n"),
                power_action("finish", summary="done"),
            ],
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        self.assertEqual(result.verification_runs, 1)

    def test_the_prompt_states_that_the_harness_ends_the_session(self) -> None:
        session, model = self.make_session(
            [power_action("finish", summary="done")],
            verification=self.passing_verification(),
        )
        session.run()
        prompt = model.prompts[0]
        self.assertIn("Apoapsis ends the session itself", prompt)
        self.assertIn("that request is refused rather than run", prompt)


class FailureEvidenceAndRepairTests(LocalPowerTestBase):
    """ADR 0070: what failed has to reach the model that must repair it.

    Reproduces the second live failure, `TASK-E01762481075`. The harness
    recorded `web-product-integrity`'s failure, carried it into the
    continuation package, and then built the next Local Power prompt without
    it. The model saw a history containing one passing check, re-ran that
    check, and reported that everything passed. Nothing here tests whether a
    model repairs well; it tests whether it is shown what to repair.
    """

    def mixed_verification(self) -> VerificationConfig:
        """The live contract's shape: a passing unit suite plus a failing
        product check that is required and acceptance-designated."""

        return VerificationConfig(
            commands=[
                VerificationCommand(
                    name="unit-tests",
                    category="tests",
                    argv=[sys.executable, "-c", "raise SystemExit(0)"],
                    timeout_seconds=60,
                ),
                VerificationCommand(
                    name="web-product-integrity",
                    category="acceptance",
                    argv=[
                        sys.executable,
                        "-c",
                        "import sys; "
                        "print(\"ERROR app.js [mode-focus]: no document defines it\"); "
                        "sys.exit(1)",
                    ],
                    timeout_seconds=60,
                    acceptance=True,
                ),
            ]
        )

    def test_a_failing_check_reaches_the_next_prompt(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="web-product-integrity"),
                power_action("read_file", path="index.html"),
            ],
            config=self.make_config(max_turns=3),
            verification=self.mixed_verification(),
        )
        session.run()
        self.assertEqual(model.actions, [])
        # Turn 3's prompt is the first one built after the failure.
        prompt = model.prompts[2]
        self.assertIn("<verification:web-product-integrity>", prompt)
        self.assertIn("mode-focus", prompt)

    def test_the_normalized_failure_is_written_to_the_audit(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="web-product-integrity"),
            ],
            config=self.make_config(max_turns=2),
            verification=self.mixed_verification(),
        )
        session.run()
        directory = self.root / ".apoapsis" / "tasks" / self.specification.task_id
        written = sorted(
            path.name
            for path in directory.glob("local-power-verification-failure-*.json")
        )
        self.assertTrue(written)
        payload = json.loads((directory / written[0]).read_text(encoding="utf-8"))
        self.assertEqual(payload["command_name"], "web-product-integrity")
        self.assertIn("mode-focus", payload["relevant_error"])

    def test_a_passing_check_produces_no_failure_evidence(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 20\n"),
                power_action("run_verification", command_name="unit"),
            ],
            verification=self.passing_verification(),
        )
        session.run()
        # The static rules mention the `<verification:NAME>` tag; what must
        # not appear is an actual evidence entry for a check that passed.
        self.assertNotIn("<verification:unit>", "\n".join(model.prompts))

    def test_the_prompt_states_which_required_commands_are_outstanding(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="unit-tests"),
                power_action("read_file", path="index.html"),
            ],
            config=self.make_config(max_turns=3),
            verification=self.mixed_verification(),
        )
        session.run()
        prompt = model.prompts[2]
        self.assertIn("OUTSTANDING_REQUIRED_COMMANDS_JSON", prompt)
        self.assertIn("'web-product-integrity' must pass and does not", prompt)
        self.assertIn('"state": "passing_for_current_code"', prompt)
        self.assertIn('"state": "never_run"', prompt)

    def test_an_edit_marks_an_earlier_pass_as_no_longer_current(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 21\n"),
                power_action("run_verification", command_name="unit"),
                power_action("write_file", path="src/app.py", content="VALUE = 22\n"),
                power_action("read_file", path="src/app.py"),
                power_action("finish", summary="done"),
            ],
            # Two required commands, so `unit` passing does not end the
            # session and the staleness of its result stays observable.
            verification=self.two_required_commands(),
        )
        session.run()
        self.assertEqual(model.actions, [])
        self.assertIn(
            '"state": "passed_earlier_but_the_code_has_changed_since"',
            model.prompts[3],
        )

    def test_finish_is_refused_while_a_required_check_was_never_run(self) -> None:
        """The live continuation, exactly: write, verify the easy check,
        re-request it, then declare victory."""

        session, model = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="unit-tests"),
                power_action("finish", summary="all checks pass"),
                power_action("run_verification", command_name="web-product-integrity"),
            ],
            config=self.make_config(max_turns=4),
            verification=self.mixed_verification(),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        refused = [
            item
            for item in result.turn_records
            if item.action == "finish" and not item.accepted
        ]
        self.assertEqual(len(refused), 1)
        self.assertIn("web-product-integrity", refused[0].summary)
        self.assertIn("has no result for the current sandbox state", refused[0].summary)
        self.assertEqual(result.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)

    def test_finish_is_allowed_once_the_model_has_edited_something(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="unit-tests"),
                power_action("write_file", path="app.js", content="// attempt\n"),
                power_action("finish", summary="tried a repair"),
            ],
            verification=self.mixed_verification(),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        self.assertEqual(
            [item.accepted for item in result.turn_records if item.action == "finish"],
            [True],
        )

    def test_finish_is_allowed_once_the_outstanding_check_has_been_run(self) -> None:
        """Running it and failing is enough. The gate exists to stop a model
        finishing without looking, not to demand success it may not have."""

        session, model = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="unit"),
                power_action("finish", summary="cannot fix this"),
            ],
            verification=self.failing_verification(),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        self.assertEqual(
            [item.accepted for item in result.turn_records if item.action == "finish"],
            [True],
        )

    def test_finish_refusals_are_capped(self) -> None:
        session, model = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="unit-tests"),
                power_action("finish", summary="done"),
                power_action("finish", summary="really done"),
                power_action("finish", summary="I insist"),
            ],
            verification=self.mixed_verification(),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        finishes = [item for item in result.turn_records if item.action == "finish"]
        self.assertEqual([item.accepted for item in finishes], [False, False, True])

    def test_a_session_with_no_changes_is_not_held_open(self) -> None:
        session, model = self.make_session(
            [power_action("finish", summary="nothing to do")],
            verification=self.mixed_verification(),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        self.assertEqual(
            [item.accepted for item in result.turn_records if item.action == "finish"],
            [True],
        )

    def test_a_resumed_session_is_told_what_failed_before(self) -> None:
        """The defect itself: a continuation that starts blind."""

        first, _ = self.make_session(
            [
                power_action("write_file", path="index.html", content="<html></html>"),
                power_action("run_verification", command_name="web-product-integrity"),
            ],
            config=self.make_config(max_turns=2),
            verification=self.mixed_verification(),
        )
        prior = first.run()
        self.assertEqual(prior.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)

        continuation = ScriptedModel([power_action("read_file", path="index.html")])
        resumed = LocalPowerSession.resume(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=self.make_config(max_turns=3),
            verification_config=self.mixed_verification(),
            audit=self.audit,
            model_call=continuation,
            prior_result=prior,
            prior_review_package=first.review_package,
        )
        resumed.run(start_turn=3)
        self.assertEqual(continuation.actions, [])
        prompt = continuation.prompts[0]
        self.assertIn("<verification:web-product-integrity>", prompt)
        self.assertIn("mode-focus", prompt)
        self.assertIn('"web-product-integrity"', prompt)


class AtomicChangeSetTests(LocalPowerTestBase):
    """ADR 0071: one turn may propose a whole coherent slice, atomically.

    The failure this addresses is not a safety failure. On live task
    `TASK-A0E17C03D69B` Qwen spent its first six turns replacing `index.html`
    alone and finished an eight-turn session with no `app.js` at all, because
    the protocol let it state one file per turn and a working web product is
    three files that have to agree. Every test here asks one of two questions:
    does a whole proposal land as a unit, and does an invalid proposal leave
    the sandbox exactly as it was?
    """

    def change_set(
        self, summary: str, changes: list[dict[str, object]], **extra: object
    ) -> str:
        return json.dumps(
            {
                "action": "propose_change_set",
                "summary": summary,
                "changes": changes,
                **extra,
            }
        )

    def slice_changes(self, *, behavior: str = "mode-focus") -> list[dict[str, object]]:
        """The three tightly coupled files the live task never got in one piece."""

        return [
            {
                "operation": "write",
                "path": "index.html",
                "content": '<html><body id="mode-focus"></body></html>\n',
            },
            {"operation": "write", "path": "styles.css", "content": "body { margin: 0 }\n"},
            {"operation": "write", "path": "app.js", "content": f"// {behavior}\n"},
        ]

    def product_verification(self) -> VerificationConfig:
        """One required, acceptance-designated check that reads the sandbox.

        Unlike a constant exit code, this one really can be repaired, so a
        failing proposal followed by a corrected proposal is a real
        pass/fail transition rather than a scripted one.
        """

        script = (
            "import pathlib, sys\n"
            "path = pathlib.Path('app.js')\n"
            "text = path.read_text(encoding='utf-8') if path.is_file() else ''\n"
            "if 'mode-focus' not in text:\n"
            "    print('ERROR app.js [mode-focus]: no document defines it')\n"
            "    sys.exit(1)\n"
        )
        return VerificationConfig(
            commands=[
                VerificationCommand(
                    name="web-product-integrity",
                    category="acceptance",
                    argv=[sys.executable, "-c", script],
                    timeout_seconds=60,
                    acceptance=True,
                )
            ]
        )

    def assert_slice_absent(self) -> None:
        for name in ("index.html", "styles.css", "app.js"):
            self.assertFalse(
                (self.root / name).exists(), f"{name} was written by a refused proposal"
            )

    # -- the whole slice lands at once --------------------------------------

    def test_one_proposal_creates_and_replaces_several_files(self) -> None:
        session, model = self.make_session(
            [
                self.change_set(
                    "focus orbit slice",
                    [
                        *self.slice_changes(),
                        {
                            "operation": "write",
                            "path": "src/app.py",
                            "content": "VALUE = 2\n",
                        },
                    ],
                ),
                power_action("finish", summary="built the slice"),
            ],
            config=self.make_config(max_turns=2),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        self.assertEqual(
            (self.root / "index.html").read_text(encoding="utf-8"),
            '<html><body id="mode-focus"></body></html>\n',
        )
        self.assertEqual((self.root / "app.js").read_text(encoding="utf-8"), "// mode-focus\n")
        # The pre-existing tracked file was replaced, not appended to.
        self.assertEqual(
            (self.root / "src" / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n"
        )
        self.assertEqual(
            sorted(result.changed_files),
            ["app.js", "index.html", "src/app.py", "styles.css"],
        )
        record = session.change_sets[0]
        self.assertTrue(record.applied)
        self.assertEqual(
            sorted((item.path, item.outcome) for item in record.operations),
            [
                ("app.js", "created"),
                ("index.html", "created"),
                ("src/app.py", "replaced"),
                ("styles.css", "created"),
            ],
        )

    def test_a_delete_and_a_write_apply_in_the_same_proposal(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "replace the module",
                    [
                        {"operation": "delete", "path": "src/app.py"},
                        {
                            "operation": "write",
                            "path": "src/replacement.py",
                            "content": "VALUE = 3\n",
                        },
                    ],
                ),
                power_action("finish", summary="swapped the module"),
            ],
            config=self.make_config(max_turns=2),
        )
        session.run()
        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertTrue((self.root / "src" / "replacement.py").is_file())

    def test_the_proposal_is_written_to_the_audit(self) -> None:
        session, _ = self.make_session(
            [self.change_set("focus orbit slice", self.slice_changes())],
            config=self.make_config(max_turns=1),
        )
        session.run()
        directory = self.root / ".apoapsis" / "tasks" / self.specification.task_id
        written = sorted(
            path.name for path in directory.glob("local-power-change-set-*.json")
        )
        self.assertEqual(written, ["local-power-change-set-001.json"])
        payload = json.loads((directory / written[0]).read_text(encoding="utf-8"))
        self.assertTrue(payload["applied"])
        self.assertEqual(len(payload["operations"]), 3)
        self.assertTrue(payload["observed_base_digest"])
        self.assertNotEqual(payload["observed_base_digest"], payload["resulting_digest"])

    # -- an invalid proposal changes nothing at all -------------------------

    def test_a_forbidden_path_rejects_the_whole_proposal(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "slice plus a peek at the environment",
                    [*self.slice_changes(), {"operation": "write", "path": ".env", "content": "X=1\n"}],
                ),
            ],
            config=self.make_config(max_turns=1),
        )
        result = session.run()
        self.assert_slice_absent()
        self.assertEqual(result.changed_files, [])
        self.assertEqual(
            (self.root / ".env").read_text(encoding="utf-8"), "API_TOKEN=super-secret\n"
        )
        self.assertEqual(len(session.rejections), 1)
        self.assertIn(".env", session.rejections[0].reason)
        self.assertFalse(session.change_sets[0].applied)

    def test_no_partial_mutation_when_one_operation_is_invalid(self) -> None:
        """The single most important property: valid-then-invalid writes nothing.

        The invalid operation is deliberately last, so a naive
        apply-as-you-validate implementation would have already written the
        two files before discovering the traversal.
        """

        session, _ = self.make_session(
            [
                self.change_set(
                    "two good files and one escape",
                    [
                        {"operation": "write", "path": "index.html", "content": "<html>\n"},
                        {"operation": "write", "path": "app.js", "content": "//\n"},
                        {
                            "operation": "write",
                            "path": "../escaped.txt",
                            "content": "outside\n",
                        },
                    ],
                ),
            ],
            config=self.make_config(max_turns=1),
        )
        session.run()
        self.assertFalse((self.root / "index.html").exists())
        self.assertFalse((self.root / "app.js").exists())
        self.assertFalse((self.root.parent / "escaped.txt").exists())

    def test_every_problem_is_reported_at_once(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "several problems",
                    [
                        {"operation": "write", "path": ".git/config", "content": "x\n"},
                        {"operation": "delete", "path": "does/not/exist.py"},
                        {"operation": "write", "path": "index.html", "content": "<html>\n"},
                    ],
                ),
            ],
            config=self.make_config(max_turns=1),
        )
        session.run()
        reason = session.rejections[0].reason
        self.assertIn(".git/config", reason)
        self.assertIn("does/not/exist.py", reason)
        self.assertIn("2 problem(s)", reason)

    def test_the_same_path_twice_is_refused(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "ambiguous intent",
                    [
                        {"operation": "write", "path": "index.html", "content": "first\n"},
                        {"operation": "write", "path": "./index.html", "content": "second\n"},
                    ],
                ),
            ],
            config=self.make_config(max_turns=1),
        )
        session.run()
        self.assertFalse((self.root / "index.html").exists())
        self.assertIn("named twice", session.rejections[0].reason)

    def test_a_verification_command_cannot_be_deleted_by_a_proposal(self) -> None:
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "test_product.py").write_text("", encoding="utf-8")
        verification = VerificationConfig(
            commands=[
                VerificationCommand(
                    name="unit",
                    category="test",
                    argv=[sys.executable, "-m", "unittest", "tests/test_product.py"],
                    timeout_seconds=60,
                )
            ]
        )
        session, _ = self.make_session(
            [
                self.change_set(
                    "remove the inconvenient check",
                    [{"operation": "delete", "path": "tests/test_product.py"}],
                ),
            ],
            config=self.make_config(max_turns=1),
            verification=verification,
        )
        session.run()
        self.assertTrue((self.root / "tests" / "test_product.py").is_file())
        self.assertIn("cannot be deleted", session.rejections[0].reason)

    def test_an_unconfigured_verification_request_rejects_the_proposal(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "slice",
                    self.slice_changes(),
                    verification_commands=["invented-check"],
                ),
            ],
            config=self.make_config(max_turns=1),
            verification=self.passing_verification(),
        )
        session.run()
        self.assert_slice_absent()
        self.assertIn("invented-check", session.rejections[0].reason)

    # -- ceilings -----------------------------------------------------------

    def test_the_per_proposal_file_ceiling_is_enforced(self) -> None:
        session, _ = self.make_session(
            [self.change_set("too wide", self.slice_changes())],
            config=self.make_config(max_turns=1, max_change_set_files=2),
        )
        session.run()
        self.assert_slice_absent()
        self.assertIn("per-proposal ceiling of 2", session.rejections[0].reason)

    def test_the_session_file_ceiling_lowers_the_proposal_ceiling(self) -> None:
        """`min` of the two, so the ceilings cannot be configured into
        disagreeing with each other."""

        session, _ = self.make_session(
            [self.change_set("too wide", self.slice_changes())],
            config=self.make_config(
                max_turns=1, max_changed_files=2, max_change_set_files=20
            ),
        )
        session.run()
        self.assert_slice_absent()
        self.assertIn("per-proposal ceiling of 2", session.rejections[0].reason)

    def test_a_changed_line_ceiling_rolls_the_whole_proposal_back(self) -> None:
        """The ceiling is only knowable after writing, so this is the rollback
        path rather than the pre-validation path."""

        session, _ = self.make_session(
            [
                self.change_set(
                    "far too many lines",
                    [
                        {
                            "operation": "write",
                            "path": "index.html",
                            "content": "line\n" * 50,
                        },
                        {"operation": "write", "path": "app.js", "content": "line\n" * 50},
                    ],
                ),
            ],
            config=self.make_config(max_turns=1, max_changed_lines=10),
        )
        session.run()
        self.assertFalse((self.root / "index.html").exists())
        self.assertFalse((self.root / "app.js").exists())
        self.assertIn("rolled back in full", session.rejections[0].reason)
        self.assertFalse(session.change_sets[0].applied)

    def test_a_rollback_restores_a_replaced_file_byte_for_byte(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "replace then blow the ceiling",
                    [
                        {
                            "operation": "write",
                            "path": "src/app.py",
                            "content": "line\n" * 80,
                        },
                    ],
                ),
            ],
            config=self.make_config(max_turns=1, max_changed_lines=5),
        )
        session.run()
        self.assertEqual(
            (self.root / "src" / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n"
        )

    # -- optimistic concurrency ---------------------------------------------

    def test_a_stale_worktree_digest_rejects_the_proposal(self) -> None:
        session, _ = self.make_session(
            [
                self.change_set(
                    "written against code I have not seen",
                    self.slice_changes(),
                    base_worktree_digest="0" * 64,
                ),
            ],
            config=self.make_config(max_turns=1),
        )
        session.run()
        self.assert_slice_absent()
        reason = session.rejections[0].reason
        self.assertIn("base_worktree_digest", reason)
        self.assertEqual(session.change_sets[0].claimed_base_digest, "0" * 64)

    def test_the_current_digest_from_the_prompt_is_accepted(self) -> None:
        session, model = self.make_session(
            [power_action("read_file", path="src/app.py"), "PLACEHOLDER"],
            config=self.make_config(max_turns=2),
        )
        digest = session._verification_state_digest()
        model.actions[1] = self.change_set(
            "slice", self.slice_changes(), base_worktree_digest=digest
        )
        session.run()
        self.assertTrue((self.root / "app.js").is_file())
        self.assertIn(digest, model.prompts[0])

    # -- verification, repair, termination ----------------------------------

    def test_the_harness_verifies_an_applied_proposal_itself(self) -> None:
        session, _ = self.make_session(
            [self.change_set("slice", self.slice_changes())],
            config=self.make_config(max_turns=1),
            verification=self.product_verification(),
        )
        result = session.run()
        self.assertEqual(len(result.verification_results), 1)
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)

    def test_all_required_commands_passing_ends_the_session(self) -> None:
        session, model = self.make_session(
            [
                self.change_set("slice", self.slice_changes()),
                power_action("read_file", path="app.js"),
            ],
            config=self.make_config(max_turns=4),
            verification=self.product_verification(),
        )
        result = session.run()
        # The second scripted action is never reached: the harness stopped.
        self.assertEqual(len(model.actions), 1)
        self.assertIn("harness ended the session", result.stop_reason)
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)

    def test_no_redundant_final_verification(self) -> None:
        session, _ = self.make_session(
            [self.change_set("slice", self.slice_changes())],
            config=self.make_config(max_turns=4),
            verification=self.product_verification(),
        )
        result = session.run()
        self.assertEqual(
            len(result.verification_results),
            1,
            "finalization re-ran a check that had already passed for this state",
        )

    def test_a_failing_proposal_is_followed_by_an_atomic_repair(self) -> None:
        session, model = self.make_session(
            [
                self.change_set("slice", self.slice_changes(behavior="mode-relax")),
                self.change_set(
                    "repair app.js only",
                    [{"operation": "write", "path": "app.js", "content": "// mode-focus\n"}],
                ),
            ],
            config=self.make_config(max_turns=4),
            verification=self.product_verification(),
        )
        result = session.run()
        self.assertEqual(model.actions, [])
        self.assertEqual(len(result.verification_results), 2)
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        self.assertTrue(all(item.applied for item in session.change_sets))
        # The repair touched one file; the other two were left alone rather
        # than regenerated, which is the behavior the delta prompt asks for.
        self.assertEqual(len(session.change_sets[1].operations), 1)

    def test_the_repair_prompt_carries_the_failure_and_what_is_outstanding(
        self,
    ) -> None:
        session, model = self.make_session(
            [
                self.change_set("slice", self.slice_changes(behavior="mode-relax")),
                power_action("read_file", path="app.js"),
            ],
            config=self.make_config(max_turns=2),
            verification=self.product_verification(),
        )
        session.run()
        prompt = model.prompts[1]
        self.assertIn("<verification:web-product-integrity>", prompt)
        self.assertIn("no document defines it", prompt)
        self.assertIn("OUTSTANDING_REQUIRED_COMMANDS_JSON", prompt)
        self.assertIn('"web-product-integrity"', prompt)
        # Delta-oriented, not a restatement of the original objective.
        self.assertIn("Do not regenerate it from the objective", prompt)
        self.assertIn("CURRENT_CHANGED_PATHS_JSON", prompt)
        self.assertIn("app.js", prompt)

    def test_a_resumed_session_can_repair_a_prior_stage_atomically(self) -> None:
        first, _ = self.make_session(
            [self.change_set("slice", self.slice_changes(behavior="mode-relax"))],
            config=self.make_config(max_turns=1),
            verification=self.product_verification(),
        )
        prior = first.run()
        self.assertEqual(prior.outcome, AgentSessionOutcome.ESCALATION_REQUIRED)

        continuation = ScriptedModel(
            [
                self.change_set(
                    "repair",
                    [{"operation": "write", "path": "app.js", "content": "// mode-focus\n"}],
                )
            ]
        )
        resumed = LocalPowerSession.resume(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=self.make_config(max_turns=3),
            verification_config=self.product_verification(),
            audit=self.audit,
            model_call=continuation,
            prior_result=prior,
            prior_review_package=first.review_package,
        )
        result = resumed.run(start_turn=2)
        prompt = continuation.prompts[0]
        self.assertIn("<verification:web-product-integrity>", prompt)
        self.assertIn("Do not regenerate it from the objective", prompt)
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)

    # -- the one-action comparison arm --------------------------------------

    def test_disabling_change_sets_removes_the_action_entirely(self) -> None:
        session, model = self.make_session(
            [
                self.change_set("slice", self.slice_changes()),
                power_action("write_file", path="index.html", content="<html>\n"),
            ],
            config=self.make_config(max_turns=2, atomic_change_sets=False),
        )
        session.run()
        self.assertEqual(session.rejections[0].action, "propose_change_set")
        self.assertIn("disabled for this session", session.rejections[0].reason)
        # The one-action protocol still works, and the prompt never offered
        # the action the harness would refuse.
        self.assertEqual(
            (self.root / "index.html").read_text(encoding="utf-8"), "<html>\n"
        )
        self.assertNotIn("propose_change_set", model.prompts[0])

    def test_the_schema_offers_change_sets_only_when_they_are_enabled(self) -> None:
        enabled = power_action_schema()
        self.assertIn("changes", enabled["properties"])
        self.assertIn("propose_change_set", enabled["properties"]["action"]["enum"])
        disabled = power_action_schema(include_change_sets=False)
        self.assertNotIn("changes", disabled["properties"])
        self.assertNotIn("propose_change_set", disabled["properties"]["action"]["enum"])


class ContractDisclosureTests(LocalPowerTestBase):
    """ADR 0069: a COMPLETE from a weak contract says so out loud."""

    def test_review_package_carries_the_contract_assessment(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 17\n"),
                power_action("run_verification", command_name="unit"),
            ],
            verification=self.passing_verification(),
        )
        session.run()
        assessment = session.review_package.contract_assessment
        self.assertIsNotNone(assessment)
        self.assertEqual(assessment.evidence_level.value, "development_only")
        self.assertIn(
            "no_acceptance_command", [item.code.value for item in assessment.findings]
        )

    def test_a_weak_contract_qualifies_the_recorded_stop_reason(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 18\n"),
                power_action("run_verification", command_name="unit"),
            ],
            verification=self.passing_verification(),
        )
        result = session.run()
        self.assertEqual(result.outcome, AgentSessionOutcome.COMPLETE)
        self.assertIn("verification-contract evidence level", result.stop_reason)
        self.assertIn("development_only", result.stop_reason)

    def test_the_assessment_is_written_as_its_own_audit_artifact(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 19\n"),
                power_action("run_verification", command_name="unit"),
            ],
            verification=self.passing_verification(),
        )
        session.run()
        path = (
            self.root
            / ".apoapsis"
            / "tasks"
            / self.specification.task_id
            / "local-power-verification-contract.json"
        )
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_level"], "development_only")


class AuditTests(LocalPowerTestBase):
    """Requirement 15: the audit record is complete enough to review from."""

    def test_audit_captures_transcript_writes_commands_rejections_and_verification(
        self,
    ) -> None:
        shell_seen: dict = {}

        def runner(argv, **kwargs):
            shell_seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "compiled\n", "")

        guard = SandboxGuard(
            self.root, forbidden_paths=LocalPowerConfig().forbidden_paths
        )
        shell = SandboxShell(
            self.root,
            policy=ShellPolicy(
                allow_shell=True,
                allow_network=False,
                timeout_seconds=30.0,
                max_output_chars=10_000,
            ),
            guard=guard,
            runner=runner,
            environ={"PATH": os.environ.get("PATH", "")},
        )
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/app.py", content="VALUE = 7\n"),
                power_action("run_shell", command="python -m compileall src"),
                power_action("write_file", path=".env", content="API_TOKEN=stolen\n"),
                power_action("finish", summary="bumped the value and compiled"),
            ],
            shell=shell,
            verification=self.passing_verification(),
        )
        session.run()

        audit_root = self.root / ".apoapsis" / "tasks" / self.specification.task_id
        names = {path.name for path in audit_root.glob("local-power-*.json")}
        self.assertTrue(any(name.startswith("local-power-turn-") for name in names))
        self.assertIn("local-power-shell-001.json", names)
        self.assertIn("local-power-rejection-001.json", names)
        self.assertIn("local-power-verification-001.json", names)
        self.assertIn("local-power-review-package.json", names)

        package = json.loads(
            (audit_root / "local-power-review-package.json").read_text(encoding="utf-8")
        )
        self.assertTrue(package["experimental"])
        self.assertEqual(len(package["transcript"]), 4)
        self.assertEqual(len(package["commands_run"]), 1)
        self.assertEqual(len(package["rejected_requests"]), 1)
        self.assertTrue(package["verification_results"])
        self.assertIn("diff --git", package["final_diff"])
        self.assertEqual(
            package["model_summary"], "bumped the value and compiled"
        )
        # The refused write is recorded, and the secret it targeted is not.
        self.assertNotIn("stolen", package["final_diff"])
        self.assertEqual(
            (self.root / ".env").read_text(encoding="utf-8"),
            "API_TOKEN=super-secret\n",
        )


class BudgetTests(LocalPowerTestBase):
    """Change-size ceilings, and what happens when one is crossed."""

    def test_a_write_that_exceeds_the_changed_file_ceiling_is_rolled_back(self) -> None:
        session, _ = self.make_session(
            [
                power_action("write_file", path="src/a.py", content="A = 1\n"),
                power_action("write_file", path="src/b.py", content="B = 1\n"),
                power_action("finish", summary="added two modules"),
            ],
            config=self.make_config(max_changed_files=1),
        )
        session.run()
        self.assertTrue((self.root / "src" / "a.py").is_file())
        self.assertFalse(
            (self.root / "src" / "b.py").exists(),
            "a budget violation must leave no trace in the sandbox",
        )
        self.assertEqual(len(session.rejections), 1)

    def test_shell_command_budget_is_enforced(self) -> None:
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "", "")

        guard = SandboxGuard(
            self.root, forbidden_paths=LocalPowerConfig().forbidden_paths
        )
        shell = SandboxShell(
            self.root,
            policy=ShellPolicy(
                allow_shell=True,
                allow_network=False,
                timeout_seconds=30.0,
                max_output_chars=1_000,
            ),
            guard=guard,
            runner=runner,
            environ={"PATH": os.environ.get("PATH", "")},
        )
        command = power_action("run_shell", command="python -m compileall src")
        session, _ = self.make_session(
            [command, command, command, power_action("finish", summary="done")],
            config=self.make_config(max_shell_commands=2),
            shell=shell,
        )
        session.run()
        self.assertEqual(len(session.commands_run), 2)
        self.assertEqual(len(session.rejections), 1)
        self.assertIn("budget is exhausted", session.rejections[0].reason)


class ActionProtocolTests(unittest.TestCase):
    """The wire protocol itself, independent of any sandbox."""

    def test_valid_actions_parse(self) -> None:
        for payload in (
            power_action("read_file", path="src/app.py"),
            power_action("search", query="AppConfig"),
            power_action("write_file", path="src/config.py", content="x = 1\n"),
            power_action("delete_file", path="src/old.py"),
            power_action("run_shell", command="python -m unittest discover"),
            power_action("finish", summary="done"),
        ):
            self.assertIsNotNone(parse_power_action(payload))

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(PowerActionError):
            parse_power_action(power_action("apply_and_complete", path="x"))

    def test_non_json_is_rejected(self) -> None:
        with self.assertRaises(PowerActionError):
            parse_power_action("here is the file you asked for")

    def test_llama_cpp_tool_residue_is_stripped_from_content(self) -> None:
        """ADR 0058's failure mode, in the new protocol's long string field."""

        action = parse_power_action(
            json.dumps(
                {
                    "action": "write_file",
                    "path": "src/config.py",
                    "content": "SETTING = True\n</arg_value></tool_call>",
                    "command_name": "unit",
                    "reason": "because",
                }
            )
        )
        self.assertEqual(action.content, "SETTING = True\n")

    def test_a_change_set_parses_into_typed_operations(self) -> None:
        action = parse_power_action(
            json.dumps(
                {
                    "action": "propose_change_set",
                    "summary": "slice",
                    "changes": [
                        {"path": "index.html", "content": "<html>\n"},
                        {"operation": "delete", "path": "old.js"},
                    ],
                }
            )
        )
        self.assertEqual(len(action.changes), 2)
        # `operation` defaults to write: a proposal that names a path and
        # supplies content means one thing only.
        self.assertEqual(action.changes[0].operation, "write")
        self.assertEqual(action.changes[1].operation, "delete")

    def test_a_write_without_content_is_rejected(self) -> None:
        with self.assertRaises(PowerActionError):
            parse_power_action(
                json.dumps(
                    {
                        "action": "propose_change_set",
                        "summary": "slice",
                        "changes": [{"operation": "write", "path": "index.html"}],
                    }
                )
            )

    def test_a_delete_carrying_content_is_rejected(self) -> None:
        with self.assertRaises(PowerActionError):
            parse_power_action(
                json.dumps(
                    {
                        "action": "propose_change_set",
                        "summary": "slice",
                        "changes": [
                            {"operation": "delete", "path": "old.js", "content": "x"}
                        ],
                    }
                )
            )

    def test_tool_residue_is_stripped_inside_a_change_set(self) -> None:
        """The residue lands in the last file's content, which under the
        one-action protocol was one corrupted file and here would be one
        corrupted file inside an otherwise correct increment."""

        action = parse_power_action(
            json.dumps(
                {
                    "action": "propose_change_set",
                    "summary": "slice",
                    "changes": [
                        {
                            "path": "app.js",
                            "content": "// real\n</arg_value></tool_call>",
                        }
                    ],
                }
            )
        )
        self.assertEqual(action.changes[0].content, "// real\n")

    def test_schema_is_a_flat_grammar_friendly_object(self) -> None:
        schema = power_action_schema()
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["action"])
        self.assertIn("finish", schema["properties"]["action"]["enum"])


class ResumeTests(LocalPowerTestBase):
    """A stopped sandbox stage can be continued rather than restarted.

    Regression: a task executed under local power reported "no prior local
    agent session exists to continue" on every continuation attempt,
    because the session record it writes (`local-power-session.json`) is
    not the filename the strict loop's reader looks for, and because the
    sandbox loop had no `resume` at all.
    """

    def _write(self, path: str, content: str) -> str:
        return json.dumps(
            {"action": "write_file", "path": path, "content": content, "reason": "r"}
        )

    def _stopped_session(self, turns: int = 2):
        session, _ = self.make_session(
            [self._write(f"src/f{index}.py", f"V = {index}\n") for index in range(turns)],
            config=self.make_config(max_turns=turns),
        )
        return session, session.run()

    def test_session_record_is_written_where_review_can_find_it(self) -> None:
        self._stopped_session()
        directory = self.root / ".apoapsis" / "tasks" / self.specification.task_id
        self.assertTrue((directory / "local-power-session.json").is_file())
        self.assertIsNotNone(read_local_power_session(directory))
        # The strict loop's reader still must not claim this stage,
        # since BoundedAgentSession cannot resume it.
        self.assertIsNone(read_agent_session(directory, ""))

    def test_read_local_stage_session_sees_a_sandbox_stage(self) -> None:
        self._stopped_session()
        directory = self.root / ".apoapsis" / "tasks" / self.specification.task_id
        session, is_local_power = read_local_stage_session(directory)
        self.assertIsNotNone(session)
        self.assertTrue(is_local_power)

    def test_resume_seeds_prior_turns_and_continues_numbering(self) -> None:
        _, prior = self._stopped_session(turns=2)
        self.assertEqual(prior.turns, 2)

        model = ScriptedModel([self._write("src/f2.py", "V = 2\n")])
        resumed = LocalPowerSession.resume(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=self.make_config(max_turns=3),
            verification_config=VerificationConfig(commands=[]),
            audit=self.audit,
            model_call=model,
            prior_result=prior,
        )
        result = resumed.run(start_turn=len(prior.turn_records) + 1)

        self.assertEqual(result.turns, 3, "prior turns must be kept, not reset")
        self.assertEqual([item.turn for item in result.turn_records], [1, 2, 3])
        self.assertEqual(model.prompts and 1, 1)

    def test_resume_restores_the_shell_budget_from_the_review_package(self) -> None:
        _, prior = self._stopped_session(turns=1)
        directory = self.root / ".apoapsis" / "tasks" / self.specification.task_id
        package = read_local_power_review_package(directory)
        self.assertIsNotNone(package)

        resumed = LocalPowerSession.resume(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=self.make_config(max_turns=2, max_shell_commands=5),
            verification_config=VerificationConfig(commands=[]),
            audit=self.audit,
            model_call=ScriptedModel([]),
            prior_result=prior,
            prior_review_package=package,
        )
        self.assertEqual(
            resumed.budget.shell_commands_used, len(package.commands_run)
        )

    def test_resume_without_a_package_does_not_invent_shell_usage(self) -> None:
        _, prior = self._stopped_session(turns=1)
        resumed = LocalPowerSession.resume(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=self.make_config(max_turns=2),
            verification_config=VerificationConfig(commands=[]),
            audit=self.audit,
            model_call=ScriptedModel([]),
            prior_result=prior,
        )
        self.assertEqual(resumed.budget.shell_commands_used, 0)

    def test_resume_carries_prior_verification_results(self) -> None:
        session, _ = self.make_session(
            [self._write("src/f0.py", "V = 0\n")],
            config=self.make_config(max_turns=1),
            verification=self.failing_verification(),
        )
        prior = session.run()
        self.assertTrue(prior.verification_results)

        resumed = LocalPowerSession.resume(
            specification=self.specification,
            worktree=self.root,
            initial_context=self.context,
            config=self.make_config(max_turns=2),
            verification_config=self.failing_verification(),
            audit=self.audit,
            model_call=ScriptedModel([]),
            prior_result=prior,
        )
        self.assertEqual(
            len(resumed.verification_results), len(prior.verification_results)
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

from apoapsis.workcell.agent_profile import (
    AgentBinaryIdentity,
    AgentExecutionProfile,
    AgentProfileEvidence,
    AgentProfileMismatch,
    ApprovalMode,
    ExpectedAgentProfile,
    ProfileFinding,
    evaluate_agent_profile,
    parse_binary_identity,
    profile_from_banner,
    require_matched_agent_profiles,
    settings_digest,
)
from apoapsis.workcell.capability_readiness import (
    CapabilityReadinessReport,
    ReadinessOperation,
    ReadinessOperationResult,
    ReadinessStatus,
    run_capability_readiness,
)

_EXE = "a" * 64
_MANIFEST = "b" * 64
_SETTINGS = "c" * 64

#: The tool set a correctly launched coding agent reports, taken from a real
#: banner captured against the image under `approvalMode: "yolo"`.
_CODING_TOOLS = [
    "agent", "edit", "glob", "grep_search", "list_directory", "read_file",
    "run_shell_command", "todo_write", "web_fetch", "write_file",
]

#: The Slice 2C surface: genuine Qwen Code, launched as a read-only planner.
_PLANNER_TOOLS = [
    "agent", "glob", "grep_search", "list_directory", "read_file",
    "todo_write", "web_fetch",
] + [f"computer_use__{name}" for name in ("click", "type_text", "launch_app")]


def _binary(**overrides) -> AgentBinaryIdentity:
    payload = {
        "resolved_path": "/usr/local/bin/qwen",
        "real_path": "/usr/local/lib/node_modules/@qwen-code/qwen-code/cli-entry.js",
        "executable_sha256": _EXE,
        "package_name": "@qwen-code/qwen-code",
        "package_version": "0.21.1",
        "package_manifest_sha256": _MANIFEST,
        "declared_entrypoint": "cli-entry.js",
        "path_candidates": 1,
    }
    payload.update(overrides)
    return AgentBinaryIdentity.model_validate(payload)


def _profile(**overrides) -> AgentExecutionProfile:
    payload = {
        "resolved_approval_mode": ApprovalMode.YOLO,
        "effective_settings_sha256": _SETTINGS,
        "realised_tools": list(_CODING_TOOLS),
        "cli_version": "0.21.1",
        "event_dialect": "stream-json",
    }
    payload.update(overrides)
    return AgentExecutionProfile.model_validate(payload)


def _evidence(arm: str = "capability_sandbox", **overrides) -> AgentProfileEvidence:
    return AgentProfileEvidence(
        arm=arm,
        binary=overrides.pop("binary", _binary()),
        profile=overrides.pop("profile", _profile()),
    )


def _expected(**overrides) -> ExpectedAgentProfile:
    payload = {
        "package_version": "0.21.1",
        "executable_sha256": _EXE,
        "package_manifest_sha256": _MANIFEST,
        "settings_sha256": _SETTINGS,
    }
    payload.update(overrides)
    return ExpectedAgentProfile.model_validate(payload)


class AgentProfileGateTests(unittest.TestCase):
    def test_a_correct_coding_profile_passes(self) -> None:
        result = evaluate_agent_profile(
            _evidence(), _expected(), readiness_proven=True
        )
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.findings, [])

    def test_the_slice_2c_planner_profile_is_rejected(self) -> None:
        # The exact failure this gate exists for: genuine binary, correct
        # version, correct hashes -- and a read-only planner.
        result = evaluate_agent_profile(
            _evidence(
                profile=_profile(
                    resolved_approval_mode=ApprovalMode.AUTO,
                    realised_tools=list(_PLANNER_TOOLS),
                )
            ),
            _expected(),
            readiness_proven=True,
        )
        self.assertFalse(result.ok)
        self.assertIn(ProfileFinding.WRONG_APPROVAL_MODE, result.findings)
        self.assertIn(ProfileFinding.MISSING_NATIVE_TOOLS, result.findings)
        self.assertIn(ProfileFinding.FORBIDDEN_TOOLS_PRESENT, result.findings)

    def test_a_binary_only_check_would_have_passed_that_run(self) -> None:
        # Stated as a test because it is the whole argument for this module:
        # every binary-identity field of the bad run was correct.
        planner = _evidence(
            profile=_profile(
                resolved_approval_mode=ApprovalMode.AUTO,
                realised_tools=list(_PLANNER_TOOLS),
            )
        )
        expected = _expected()
        self.assertEqual(planner.binary.package_name, expected.package_name)
        self.assertEqual(planner.binary.package_version, expected.package_version)
        self.assertEqual(planner.binary.executable_sha256, expected.executable_sha256)
        self.assertEqual(
            planner.binary.package_manifest_sha256, expected.package_manifest_sha256
        )
        # ...and the gate still refuses it.
        self.assertFalse(
            evaluate_agent_profile(planner, expected, readiness_proven=True).ok
        )

    def test_each_missing_native_tool_class_is_caught(self) -> None:
        for dropped in ("read_file", "write_file", "edit", "run_shell_command"):
            tools = [item for item in _CODING_TOOLS if item != dropped]
            result = evaluate_agent_profile(
                _evidence(profile=_profile(realised_tools=tools)),
                _expected(),
                readiness_proven=True,
            )
            self.assertIn(
                ProfileFinding.MISSING_NATIVE_TOOLS, result.findings, dropped
            )

    def test_replace_satisfies_the_edit_requirement(self) -> None:
        # `replace` was renamed `edit` in 0.21.1; either is a real edit tool.
        tools = [item for item in _CODING_TOOLS if item != "edit"] + ["replace"]
        result = evaluate_agent_profile(
            _evidence(profile=_profile(realised_tools=tools)),
            _expected(),
            readiness_proven=True,
        )
        self.assertTrue(result.ok, result.detail)

    def test_wrong_package_version_and_hashes_are_caught(self) -> None:
        cases = {
            ProfileFinding.WRONG_PACKAGE: {"package_name": "@other/cli"},
            ProfileFinding.WRONG_VERSION: {"package_version": "0.22.0"},
            ProfileFinding.UNPINNED_EXECUTABLE: {"executable_sha256": "d" * 64},
            ProfileFinding.UNPINNED_PACKAGE_MANIFEST: {
                "package_manifest_sha256": "e" * 64
            },
            ProfileFinding.WRONG_ENTRYPOINT: {"real_path": "/usr/bin/something-else"},
            ProfileFinding.SHADOWED_EXECUTABLE: {"path_candidates": 2},
        }
        for finding, override in cases.items():
            result = evaluate_agent_profile(
                _evidence(binary=_binary(**override)),
                _expected(),
                readiness_proven=True,
            )
            self.assertIn(finding, result.findings, finding.value)

    def test_settings_digest_and_dialect_mismatches_are_caught(self) -> None:
        self.assertIn(
            ProfileFinding.WRONG_SETTINGS_DIGEST,
            evaluate_agent_profile(
                _evidence(profile=_profile(effective_settings_sha256="f" * 64)),
                _expected(),
                readiness_proven=True,
            ).findings,
        )
        self.assertIn(
            ProfileFinding.WRONG_EVENT_DIALECT,
            evaluate_agent_profile(
                _evidence(profile=_profile(event_dialect="terminal-text")),
                _expected(),
                readiness_proven=True,
            ).findings,
        )

    def test_unexpected_mcp_servers_are_caught(self) -> None:
        self.assertIn(
            ProfileFinding.UNEXPECTED_MCP_SERVERS,
            evaluate_agent_profile(
                _evidence(profile=_profile(mcp_servers=["something"])),
                _expected(),
                readiness_proven=True,
            ).findings,
        )

    def test_readiness_must_be_proven_not_merely_registered(self) -> None:
        for proven in (None, False):
            result = evaluate_agent_profile(
                _evidence(), _expected(), readiness_proven=proven
            )
            self.assertFalse(result.ok)
            self.assertIn(ProfileFinding.READINESS_NOT_PROVEN, result.findings)

    def test_the_raising_form_aborts_before_inference(self) -> None:
        from apoapsis.workcell.agent_profile import require_agent_profile

        with self.assertRaises(AgentProfileMismatch):
            require_agent_profile(
                _evidence(profile=_profile(resolved_approval_mode=ApprovalMode.AUTO)),
                _expected(),
                readiness_proven=True,
            )


class MatchedArmTests(unittest.TestCase):
    def test_identical_arms_match(self) -> None:
        result = require_matched_agent_profiles(
            _evidence("default_qwen_control"),
            _evidence("capability_sandbox"),
            _expected(),
            control_readiness=True,
            candidate_readiness=True,
        )
        self.assertTrue(result.ok, result.detail)

    def test_arms_running_different_profiles_are_refused(self) -> None:
        control = _evidence(
            "default_qwen_control",
            profile=_profile(realised_tools=[i for i in _CODING_TOOLS if i != "agent"]),
        )
        result = require_matched_agent_profiles(
            control,
            _evidence("capability_sandbox"),
            _expected(),
            control_readiness=True,
            candidate_readiness=True,
        )
        self.assertFalse(result.ok)
        self.assertIn(ProfileFinding.ARMS_DISAGREE, result.findings)
        self.assertIn("agent", result.detail)

    def test_one_bad_arm_fails_the_pair(self) -> None:
        result = require_matched_agent_profiles(
            _evidence("default_qwen_control"),
            _evidence("capability_sandbox"),
            _expected(),
            control_readiness=True,
            candidate_readiness=False,
        )
        self.assertFalse(result.ok)
        self.assertIn(ProfileFinding.READINESS_NOT_PROVEN, result.findings)

    def test_the_comparable_digest_ignores_task_and_workspace(self) -> None:
        # Arms differ by task by construction; that must not make them
        # incomparable as agents.
        self.assertEqual(
            _evidence("default_qwen_control").comparable_digest(),
            _evidence("capability_sandbox").comparable_digest(),
        )


class BannerParsingTests(unittest.TestCase):
    def _banner(self, **overrides) -> dict:
        payload = {
            "type": "system",
            "subtype": "init",
            "tools": list(_CODING_TOOLS),
            "permission_mode": "yolo",
            "qwen_code_version": "0.21.1",
            "mcp_servers": [],
        }
        payload.update(overrides)
        return payload

    def test_a_banner_becomes_a_profile(self) -> None:
        profile = profile_from_banner(
            self._banner(), effective_settings_sha256=_SETTINGS
        )
        self.assertEqual(profile.resolved_approval_mode, ApprovalMode.YOLO)
        self.assertEqual(profile.cli_version, "0.21.1")
        self.assertIn("run_shell_command", profile.realised_tools)

    def test_the_real_slice_2c_banner_parses_as_auto(self) -> None:
        profile = profile_from_banner(
            self._banner(permission_mode="auto", tools=list(_PLANNER_TOOLS)),
            effective_settings_sha256=_SETTINGS,
        )
        self.assertEqual(profile.resolved_approval_mode, ApprovalMode.AUTO)

    def test_a_banner_without_tools_or_mode_is_refused(self) -> None:
        for override in ({"tools": []}, {"permission_mode": None}, {"tools": None}):
            with self.assertRaises(AgentProfileMismatch):
                profile_from_banner(
                    self._banner(**override), effective_settings_sha256=_SETTINGS
                )

    def test_an_unrecognised_permission_mode_is_refused(self) -> None:
        with self.assertRaises(AgentProfileMismatch):
            profile_from_banner(
                self._banner(permission_mode="wide-open"),
                effective_settings_sha256=_SETTINGS,
            )

    def test_provenance_json_parses_and_incomplete_output_is_refused(self) -> None:
        good = json.dumps(
            {
                "resolved_path": "/usr/local/bin/qwen",
                "real_path": "/x/cli-entry.js",
                "executable_sha256": _EXE,
                "package_manifest_sha256": _MANIFEST,
                "path_candidates": 1,
                "package_name": "@qwen-code/qwen-code",
                "package_version": "0.21.1",
                "declared_entrypoint": "cli-entry.js",
            }
        )
        self.assertEqual(parse_binary_identity(good).package_version, "0.21.1")
        with self.assertRaises(AgentProfileMismatch):
            parse_binary_identity("no json here")
        holed = json.loads(good)
        holed["executable_sha256"] = None
        with self.assertRaises(AgentProfileMismatch):
            parse_binary_identity(json.dumps(holed))

    def test_settings_digest_is_formatting_independent(self) -> None:
        self.assertEqual(
            settings_digest({"a": 1, "b": {"c": 2}}),
            settings_digest({"b": {"c": 2}, "a": 1}),
        )
        self.assertNotEqual(
            settings_digest({"a": 1}), settings_digest({"a": 2})
        )


class _FakeShell:
    """A workcell whose filesystem behaves, or refuses in one specific way."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.files: dict[str, str] = {}

    def __call__(self, argv, timeout):
        joined = " ".join(argv)
        if self.fail and self.fail in joined:
            return 1, "", "refused"
        if "> /workspace" in joined and "printf" in joined:
            self.files["probe"] = "apoapsis-readiness-initial"
            return 0, "ok\n", ""
        if argv[0] == "cat":
            return 0, self.files.get("probe", "") + "\n", ""
        if "sed -i" in joined:
            self.files["probe"] = self.files.get("probe", "").replace(
                "initial", "edited"
            )
            return 0, self.files["probe"] + "\n", ""
        if "grep -c edited" in joined:
            return (0, "1\n", "") if "edited" in self.files.get("probe", "") else (1, "0\n", "")
        if "rm -f" in joined:
            self.files.pop("probe", None)
            return 0, "gone\n", ""
        return 0, "", ""


class CapabilityReadinessTests(unittest.TestCase):
    def test_a_working_workcell_is_ready(self) -> None:
        report = run_capability_readiness(_FakeShell())
        # HOST_VISIBLE stays NOT_RUN without a host path, and NOT_RUN is not a
        # pass, so readiness is withheld.
        self.assertFalse(report.ready)
        self.assertEqual(
            report.result(ReadinessOperation.HOST_VISIBLE).status,
            ReadinessStatus.NOT_RUN,
        )
        for operation in (
            ReadinessOperation.WORKSPACE_WRITABLE,
            ReadinessOperation.READ,
            ReadinessOperation.EDIT,
            ReadinessOperation.SHELL,
            ReadinessOperation.CLEANUP,
        ):
            self.assertEqual(
                report.result(operation).status, ReadinessStatus.PASSED, operation.value
            )

    def test_an_unwritable_clone_stops_immediately(self) -> None:
        report = run_capability_readiness(_FakeShell(fail="printf"))
        self.assertFalse(report.ready)
        self.assertEqual(
            report.result(ReadinessOperation.WORKSPACE_WRITABLE).status,
            ReadinessStatus.FAILED,
        )
        # Later operations are not attempted, and are reported as such rather
        # than as passes.
        self.assertEqual(
            report.result(ReadinessOperation.EDIT).status, ReadinessStatus.NOT_RUN
        )

    def test_a_failing_edit_is_caught(self) -> None:
        report = run_capability_readiness(_FakeShell(fail="sed"))
        self.assertFalse(report.ready)
        self.assertEqual(
            report.result(ReadinessOperation.EDIT).status, ReadinessStatus.FAILED
        )

    def test_a_failing_shell_is_caught(self) -> None:
        report = run_capability_readiness(_FakeShell(fail="grep -c"))
        self.assertFalse(report.ready)
        self.assertEqual(
            report.result(ReadinessOperation.SHELL).status, ReadinessStatus.FAILED
        )

    def test_residue_left_behind_withholds_readiness(self) -> None:
        # A probe artifact surviving into the clone would appear in the
        # computed delta as work the agent did not do.
        report = run_capability_readiness(_FakeShell(fail="rm -f"))
        self.assertFalse(report.ready)
        self.assertFalse(report.residue_free)

    def test_an_incomplete_report_is_never_ready(self) -> None:
        report = CapabilityReadinessReport(
            results=[
                ReadinessOperationResult(
                    operation=ReadinessOperation.READ, status=ReadinessStatus.PASSED
                )
            ],
            ready=False,
            detail="partial",
        )
        self.assertFalse(report.ready)


if __name__ == "__main__":
    unittest.main()

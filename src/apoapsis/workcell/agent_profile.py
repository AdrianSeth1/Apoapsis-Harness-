"""Execution-profile identity: is the agent under test the agent we mean?

Slice 2C measured two arms, scored them, and produced `CAPABILITY_REGRESSED`.
The verdict was worthless, and the reason is the one this module exists to make
impossible ever again.

The binary was genuine. `/usr/local/bin/qwen` really did resolve to
`@qwen-code/qwen-code@0.21.1`, correct symlink, correct package, correct
repository. `write_file` and `run_shell_command` really were present in the
bundle. A binary-provenance check would have passed cleanly and told us
nothing.

What was wrong was the *execution profile*. Qwen Code's headless `-p` path runs
under Plan Mode semantics, and its default `tools.approvalMode` of `auto`
registers no mutating tools at all in non-interactive use. Its
`tools.computerUse.enabled` defaults to `true` and adds 35 `computer_use__*`
tools. So both arms ran a **read-only planner with a desktop-automation
toolbelt**, not the coding agent being evaluated. The control arm was observed
calling `computer_use__launch_app` on a task that asked it to edit `calc.py`.

That is an **execution-profile identity failure**, and the distinction matters:

* binary identity answers "is this the right program?"
* execution-profile identity answers "is this the right program, launched as
  the thing we are measuring?"

Only the second would have caught it. So the gate below requires both, plus a
realised tool set observed from the agent's own session banner, plus proof that
the two arms ran the same profile as each other. Any mismatch aborts **before
inference** — the previous run spent roughly 940,000 input tokens establishing
nothing.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel

_SHA256_HEX = r"^[0-9a-f]{64}$"


class ApprovalMode(StrEnum):
    """Qwen Code's five permission modes, as its own documentation names them."""

    PLAN = "plan"
    #: Historically "default"; the docs renamed the label, not the value.
    DEFAULT = "default"
    AUTO_EDIT = "auto-edit"
    AUTO = "auto"
    YOLO = "yolo"


#: The only mode under which the headless CLI registers file editing *and*
#: shell. Measured directly against the real image, one mode at a time:
#:
#: | mode      | write_file | edit | run_shell_command |
#: | --------- | ---------- | ---- | ----------------- |
#: | auto      | absent     | abs. | absent            |
#: | auto-edit | present    | pres.| absent            |
#: | yolo      | present    | pres.| present           |
#:
#: `auto` is the shipped default, which is why nobody noticed.
#:
#: `yolo` is only defensible because ADR 0077 already put the agent inside a
#: disposable, `--network none` container whose sole egress is a controller-owned
#: socket. The container is the safety boundary; the approval prompt never was.
#: This constant must not be relaxed for a workcell that is not so confined.
REQUIRED_APPROVAL_MODE = ApprovalMode.YOLO

#: Native tools the coding agent must actually have. Names are the 0.21.1 ones:
#: `replace` was renamed `edit`, so either satisfies the edit requirement.
REQUIRED_READ_TOOLS: frozenset[str] = frozenset({"read_file"})
REQUIRED_WRITE_TOOLS: frozenset[str] = frozenset({"write_file"})
REQUIRED_EDIT_TOOLS: frozenset[str] = frozenset({"edit", "replace"})
REQUIRED_SHELL_TOOLS: frozenset[str] = frozenset({"run_shell_command"})

#: Tools whose presence means the profile is wrong. A coding agent in a
#: headless Linux container has no screen to click on, and 35 of these crowd
#: out the tools it does need.
FORBIDDEN_TOOL_PREFIXES: tuple[str, ...] = ("computer_use__",)

#: The settings the coding profile requires, as the owner fixed them. Written
#: here rather than in a JSON file so the gate and the launcher cannot drift.
CODING_PROFILE_TOOL_SETTINGS: dict[str, object] = {
    "approvalMode": REQUIRED_APPROVAL_MODE.value,
    "computerUse": {"enabled": False},
    # Disabled for a stable prompt prefix: the handoff wants prefix-KV cache
    # locality, and Qwen Code's own settings reference recommends turning
    # ToolSearch off for models that rely on it. It also makes the realised
    # tool set explicit rather than discovered mid-session.
    "toolSearch": {"enabled": False},
}


def settings_digest(settings: dict) -> str:
    """One stable hash for a settings document.

    Sorted keys and tight separators, so a reformatted file that means the same
    thing hashes the same, and a changed value never does.
    """

    return hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class AgentBinaryIdentity(StrictModel):
    """What program is on disk, and where it came from."""

    #: What `command -v qwen` returned.
    resolved_path: str = Field(min_length=1)
    #: What that resolves to after following every symlink.
    real_path: str = Field(min_length=1)
    executable_sha256: str = Field(pattern=_SHA256_HEX)
    package_name: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    package_manifest_sha256: str = Field(pattern=_SHA256_HEX)
    #: The `bin` entry the package declares, e.g. `cli-entry.js`.
    declared_entrypoint: str = Field(min_length=1)
    #: How many distinct `qwen` executables were found on `PATH`. More than one
    #: means something could shadow the pinned install depending on order.
    path_candidates: int = Field(default=1, ge=0)


class AgentExecutionProfile(StrictModel):
    """How that program was launched, observed from its own session banner.

    Every field here is read back from the running agent rather than from the
    file we wrote. Asserting the contents of a settings file proves only that
    we can read our own writing.
    """

    resolved_approval_mode: ApprovalMode
    effective_settings_sha256: str = Field(pattern=_SHA256_HEX)
    #: The tool names the agent itself reported at session start.
    realised_tools: list[str] = Field(min_length=1)
    cli_version: str = Field(min_length=1)
    #: `stream-json` is the only dialect the adapter is conformance-tested
    #: against; terminal scraping is not a testable interface.
    event_dialect: str = Field(min_length=1)
    mcp_servers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_tools_sorted(self) -> AgentExecutionProfile:
        # Sorted so the digest below is order-independent, matching how
        # `AgentCliPin.tool_names` is treated.
        object.__setattr__(self, "realised_tools", sorted(set(self.realised_tools)))
        return self

    @property
    def realised_tool_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.realised_tools, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class AgentProfileEvidence(StrictModel):
    """One arm's complete agent-identity evidence."""

    arm: str = Field(min_length=1)
    binary: AgentBinaryIdentity
    profile: AgentExecutionProfile

    def comparable_digest(self) -> str:
        """Everything that must be identical between the two arms.

        Deliberately excludes the task, the prompt, and the workspace: those
        differ by construction. What must not differ is which agent ran and how
        it was launched.
        """

        payload = {
            "executable_sha256": self.binary.executable_sha256,
            "package_name": self.binary.package_name,
            "package_version": self.binary.package_version,
            "package_manifest_sha256": self.binary.package_manifest_sha256,
            "declared_entrypoint": self.binary.declared_entrypoint,
            "approval_mode": self.profile.resolved_approval_mode.value,
            "effective_settings_sha256": self.profile.effective_settings_sha256,
            "realised_tools": self.profile.realised_tools,
            "cli_version": self.profile.cli_version,
            "event_dialect": self.profile.event_dialect,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class ProfileFinding(StrEnum):
    WRONG_PACKAGE = "wrong_package"
    WRONG_VERSION = "wrong_version"
    UNPINNED_EXECUTABLE = "unpinned_executable"
    UNPINNED_PACKAGE_MANIFEST = "unpinned_package_manifest"
    WRONG_ENTRYPOINT = "wrong_entrypoint"
    SHADOWED_EXECUTABLE = "shadowed_executable"
    WRONG_APPROVAL_MODE = "wrong_approval_mode"
    WRONG_SETTINGS_DIGEST = "wrong_settings_digest"
    MISSING_NATIVE_TOOLS = "missing_native_tools"
    FORBIDDEN_TOOLS_PRESENT = "forbidden_tools_present"
    WRONG_EVENT_DIALECT = "wrong_event_dialect"
    UNEXPECTED_MCP_SERVERS = "unexpected_mcp_servers"
    ARMS_DISAGREE = "arms_disagree"
    READINESS_NOT_PROVEN = "readiness_not_proven"


class ProfileGateResult(StrictModel):
    schema_version: str = "1.0"
    ok: bool = False
    findings: list[ProfileFinding] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    comparable_digest: str | None = None
    detail: str = Field(min_length=1)


class AgentProfileMismatch(RuntimeError):
    """Raised before inference when the agent under test is the wrong one."""


class ExpectedAgentProfile(StrictModel):
    """What the run is pinned to. Every field is required."""

    package_name: str = Field(default="@qwen-code/qwen-code", min_length=1)
    package_version: str = Field(min_length=1)
    executable_sha256: str = Field(pattern=_SHA256_HEX)
    package_manifest_sha256: str = Field(pattern=_SHA256_HEX)
    declared_entrypoint: str = Field(default="cli-entry.js", min_length=1)
    settings_sha256: str = Field(pattern=_SHA256_HEX)
    approval_mode: ApprovalMode = REQUIRED_APPROVAL_MODE
    event_dialect: str = Field(default="stream-json", min_length=1)


def evaluate_agent_profile(
    evidence: AgentProfileEvidence,
    expected: ExpectedAgentProfile,
    *,
    readiness_proven: bool | None = None,
) -> ProfileGateResult:
    """Check one arm's agent identity. Fails closed on every uncertainty.

    `readiness_proven=None` means the sacrificial read/edit/shell exercise was
    not run, which is a failure rather than an omission: a realised tool list is
    a claim about registration, not about the tools working.
    """

    findings: list[ProfileFinding] = []
    details: list[str] = []

    def fail(code: ProfileFinding, message: str) -> None:
        findings.append(code)
        details.append(message)

    binary = evidence.binary
    profile = evidence.profile

    if binary.package_name != expected.package_name:
        fail(
            ProfileFinding.WRONG_PACKAGE,
            f"the agent belongs to {binary.package_name!r}, not "
            f"{expected.package_name!r}",
        )
    if binary.package_version != expected.package_version:
        fail(
            ProfileFinding.WRONG_VERSION,
            f"agent version {binary.package_version!r} is not the pinned "
            f"{expected.package_version!r}",
        )
    if binary.executable_sha256 != expected.executable_sha256:
        fail(
            ProfileFinding.UNPINNED_EXECUTABLE,
            f"the resolved executable hashes to {binary.executable_sha256}, but "
            f"the run pins {expected.executable_sha256}",
        )
    if binary.package_manifest_sha256 != expected.package_manifest_sha256:
        fail(
            ProfileFinding.UNPINNED_PACKAGE_MANIFEST,
            "the package manifest hash does not match the pinned one, so the "
            "installed package is not the one this run was qualified against",
        )
    if not binary.real_path.endswith(expected.declared_entrypoint):
        fail(
            ProfileFinding.WRONG_ENTRYPOINT,
            f"{binary.resolved_path} resolves to {binary.real_path}, which is "
            f"not the declared entrypoint {expected.declared_entrypoint!r}",
        )
    if binary.path_candidates > 1:
        # Not necessarily malicious, but it means PATH order decides which
        # agent runs, and PATH order is not part of the manifest.
        fail(
            ProfileFinding.SHADOWED_EXECUTABLE,
            f"{binary.path_candidates} `qwen` executables are on PATH, so which "
            "one runs depends on PATH order rather than on the pin",
        )

    if profile.resolved_approval_mode != expected.approval_mode:
        fail(
            ProfileFinding.WRONG_APPROVAL_MODE,
            f"the agent resolved approval mode "
            f"{profile.resolved_approval_mode.value!r}, not "
            f"{expected.approval_mode.value!r}. This is the Slice 2C failure: "
            "under any other mode the headless CLI is a read-only planner, not "
            "the coding agent being evaluated.",
        )
    if profile.effective_settings_sha256 != expected.settings_sha256:
        fail(
            ProfileFinding.WRONG_SETTINGS_DIGEST,
            "the agent's effective settings do not hash to the pinned value",
        )
    if profile.event_dialect != expected.event_dialect:
        fail(
            ProfileFinding.WRONG_EVENT_DIALECT,
            f"event dialect {profile.event_dialect!r} is not the "
            f"conformance-tested {expected.event_dialect!r}",
        )

    tools = set(profile.realised_tools)
    missing: list[str] = []
    for label, required in (
        ("read", REQUIRED_READ_TOOLS),
        ("write", REQUIRED_WRITE_TOOLS),
        ("edit", REQUIRED_EDIT_TOOLS),
        ("shell", REQUIRED_SHELL_TOOLS),
    ):
        if not (tools & required):
            missing.append(f"{label} (any of: {', '.join(sorted(required))})")
    if missing:
        fail(
            ProfileFinding.MISSING_NATIVE_TOOLS,
            "the agent registered no " + "; no ".join(missing),
        )

    forbidden = sorted(
        name
        for name in tools
        if name.startswith(FORBIDDEN_TOOL_PREFIXES)
    )
    if forbidden:
        fail(
            ProfileFinding.FORBIDDEN_TOOLS_PRESENT,
            f"{len(forbidden)} desktop-automation tool(s) are registered "
            f"(e.g. {', '.join(forbidden[:3])}); the coding profile disables "
            "them, and their presence means the profile did not apply",
        )
    if profile.mcp_servers:
        fail(
            ProfileFinding.UNEXPECTED_MCP_SERVERS,
            f"unexpected MCP servers are attached: {', '.join(profile.mcp_servers)}",
        )

    if readiness_proven is not True:
        fail(
            ProfileFinding.READINESS_NOT_PROVEN,
            "the sacrificial read/edit/shell exercise did not succeed, so the "
            "tools are registered but not demonstrated to work"
            if readiness_proven is False
            else "the sacrificial read/edit/shell exercise was not run; a "
            "registered tool is a claim, not a demonstration",
        )

    if findings:
        return ProfileGateResult(
            ok=False,
            findings=findings,
            details=details,
            comparable_digest=evidence.comparable_digest(),
            detail=(
                f"agent profile rejected for arm {evidence.arm!r} with "
                f"{len(findings)} finding(s): " + "; ".join(details)
            ),
        )
    return ProfileGateResult(
        ok=True,
        comparable_digest=evidence.comparable_digest(),
        detail=(
            f"arm {evidence.arm!r} is running {binary.package_name}"
            f"@{binary.package_version} as a coding agent: approval mode "
            f"{profile.resolved_approval_mode.value}, "
            f"{len(profile.realised_tools)} tools including read, write, edit, "
            "and shell, no desktop-automation tools, and the sacrificial "
            "exercise succeeded"
        ),
    )


def require_matched_agent_profiles(
    control: AgentProfileEvidence,
    candidate: AgentProfileEvidence,
    expected: ExpectedAgentProfile,
    *,
    control_readiness: bool | None = None,
    candidate_readiness: bool | None = None,
) -> ProfileGateResult:
    """Both arms must be the same agent, launched the same way.

    A paired comparison in which the two arms ran different agents measures the
    difference between the agents, and reports it as a difference between the
    harnesses.
    """

    left = evaluate_agent_profile(control, expected, readiness_proven=control_readiness)
    right = evaluate_agent_profile(
        candidate, expected, readiness_proven=candidate_readiness
    )
    findings = list(left.findings) + list(right.findings)
    details = list(left.details) + list(right.details)

    left_digest = control.comparable_digest()
    right_digest = candidate.comparable_digest()
    if left_digest != right_digest:
        findings.append(ProfileFinding.ARMS_DISAGREE)
        differing = sorted(
            set(control.profile.realised_tools) ^ set(candidate.profile.realised_tools)
        )
        details.append(
            "the two arms did not run the same agent profile "
            f"({left_digest[:12]} vs {right_digest[:12]})"
            + (
                f"; tool sets differ by: {', '.join(differing[:6])}"
                if differing
                else "; the difference is not in the tool set"
            )
        )

    if findings:
        return ProfileGateResult(
            ok=False,
            findings=findings,
            details=details,
            detail=(
                f"the paired arms are not a valid agent comparison: "
                + "; ".join(details)
            ),
        )
    return ProfileGateResult(
        ok=True,
        comparable_digest=left_digest,
        detail=(
            "both arms ran the identical agent profile "
            f"({left_digest[:12]}), so a capability difference between them can "
            "be attributed to the harness rather than to the agent"
        ),
    )


#: Emitted as JSON from inside the workcell. Read-only: it inspects, hashes,
#: and reports, and touches nothing. Kept as source here rather than as a file
#: in the image so the gate and the thing it measures cannot drift apart.
PROVENANCE_SCRIPT = r"""
set -e
Q=$(command -v qwen 2>/dev/null || true)
R=$(readlink -f "$Q" 2>/dev/null || true)
N=0
OLDIFS=$IFS; IFS=:
for d in $PATH; do [ -e "$d/qwen" ] && N=$((N+1)); done
IFS=$OLDIFS
PKG=/usr/local/lib/node_modules/@qwen-code/qwen-code/package.json
python3 - "$Q" "$R" "$N" "$PKG" <<'PYEOF'
import hashlib, json, os, sys
resolved, real, candidates, manifest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
def sha(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
doc = {
    "resolved_path": resolved or None,
    "real_path": real or None,
    "executable_sha256": sha(real) if real else None,
    "package_manifest_sha256": sha(manifest),
    "path_candidates": int(candidates or 0),
}
try:
    with open(manifest, encoding="utf-8") as handle:
        pkg = json.load(handle)
    doc["package_name"] = pkg.get("name")
    doc["package_version"] = pkg.get("version")
    binv = pkg.get("bin") or {}
    doc["declared_entrypoint"] = binv.get("qwen") if isinstance(binv, dict) else None
except (OSError, ValueError):
    doc["package_name"] = doc["package_version"] = doc["declared_entrypoint"] = None
print(json.dumps(doc))
PYEOF
"""


def parse_binary_identity(stdout: str) -> AgentBinaryIdentity:
    """Turn the provenance script's JSON into a binary identity.

    Any missing field raises rather than defaulting: an identity gate whose
    inputs can be absent is not a gate.
    """

    line = next(
        (item for item in reversed(stdout.strip().splitlines()) if item.startswith("{")),
        None,
    )
    if line is None:
        raise AgentProfileMismatch(
            "the provenance probe produced no JSON, so the agent binary could "
            f"not be identified: {stdout.strip()[:200]!r}"
        )
    payload = json.loads(line)
    missing = [key for key, value in payload.items() if value is None]
    if missing:
        raise AgentProfileMismatch(
            "the agent binary could not be fully identified; unresolved: "
            + ", ".join(sorted(missing))
        )
    return AgentBinaryIdentity.model_validate(payload)


def profile_from_banner(
    banner: dict, *, effective_settings_sha256: str, event_dialect: str = "stream-json"
) -> AgentExecutionProfile:
    """Read the execution profile from the agent's own session banner.

    The banner is emitted before the first inference request, so this costs
    nothing and — crucially — reports what the agent *resolved*, not what we
    asked for. Slice 2C's settings file and the agent's behaviour disagreed,
    and only the banner would have shown it.
    """

    tools = banner.get("tools")
    if not isinstance(tools, list) or not tools:
        raise AgentProfileMismatch(
            "the agent's session banner declared no tools, so its execution "
            "profile cannot be established"
        )
    mode = banner.get("permission_mode")
    if not isinstance(mode, str):
        raise AgentProfileMismatch(
            "the agent's session banner declared no permission mode"
        )
    try:
        approval = ApprovalMode(mode)
    except ValueError as exc:
        raise AgentProfileMismatch(
            f"the agent reported an unrecognised permission mode {mode!r}"
        ) from exc
    version = banner.get("qwen_code_version")
    if not isinstance(version, str) or not version:
        raise AgentProfileMismatch("the agent's session banner declared no version")
    servers = banner.get("mcp_servers") or []
    return AgentExecutionProfile(
        resolved_approval_mode=approval,
        effective_settings_sha256=effective_settings_sha256,
        realised_tools=[str(item) for item in tools],
        cli_version=version,
        event_dialect=event_dialect,
        mcp_servers=[
            str(item.get("name") if isinstance(item, dict) else item)
            for item in servers
        ],
    )


def require_agent_profile(
    evidence: AgentProfileEvidence,
    expected: ExpectedAgentProfile,
    *,
    readiness_proven: bool | None = None,
) -> ProfileGateResult:
    """The raising form. Call this before spending a single token."""

    result = evaluate_agent_profile(
        evidence, expected, readiness_proven=readiness_proven
    )
    if not result.ok:
        raise AgentProfileMismatch(result.detail)
    return result

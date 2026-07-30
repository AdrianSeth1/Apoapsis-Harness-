"""Containment probes: proof, from inside the box, that the box is closed.

ADR 0077 permits a real shell inside the workcell on the strength of one claim:
that the container boundary already denies everything the action grammar was
denying. That claim is worth exactly as much as the evidence for it, so this
module runs the checks from *inside* the container and fails closed on anything
it could not establish.

Two design rules matter more than the probe list:

**An unrun probe is never a pass.** Every probe starts `NOT_RUN`, and
`evaluate_containment` reports `NOT_RUN` as a containment failure. The most
likely way to ship a broken sandbox is a probe that silently did not execute.

**Each probe states what a breach looks like, not what safety looks like.** A
probe passes when the forbidden thing was *observably* refused. A command that
fails for an unrelated reason — a missing binary, a typo — is `INCONCLUSIVE`,
which also fails the gate. "It errored, so we must be safe" is how a hole stays
open.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel


class ContainmentCategory(StrEnum):
    HOST_FILESYSTEM = "host_filesystem"
    CREDENTIALS = "credentials"
    NETWORK = "network"
    CONTAINER_CONTROL = "container_control"
    CONTROLLER_AUTHORITY = "controller_authority"
    PRIVILEGE = "privilege"
    RESOURCE_CEILING = "resource_ceiling"


class ProbeExpectation(StrEnum):
    """What counts as containment for this probe."""

    #: The path must not exist.
    PATH_ABSENT = "path_absent"
    #: The path may exist but must not be writable.
    PATH_NOT_WRITABLE = "path_not_writable"
    #: The command must fail (non-zero exit).
    COMMAND_REFUSED = "command_refused"
    #: The command must succeed. Used for the two positive checks: a mitigation
    #: that is switched on, and a capability the agent is supposed to have.
    COMMAND_SUCCEEDS = "command_succeeds"
    #: The command must succeed and its stdout must match `expected_stdout`.
    STDOUT_EQUALS = "stdout_equals"


class ProbeStatus(StrEnum):
    #: The forbidden thing was observably refused.
    CONTAINED = "contained"
    #: The forbidden thing was reachable. This is a breach.
    BREACHED = "breached"
    #: The probe ran but its result does not establish either. Fails the gate.
    INCONCLUSIVE = "inconclusive"
    #: The probe never executed. Fails the gate.
    NOT_RUN = "not_run"


class ContainmentProbe(StrictModel):
    probe_id: str = Field(min_length=1)
    category: ContainmentCategory
    #: Executed inside the workcell, never through a shell.
    argv: list[str] = Field(min_length=1)
    expectation: ProbeExpectation
    expected_stdout: str | None = None
    #: What a breach would mean, in the owner's terms.
    breach_meaning: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expected_stdout(self) -> ContainmentProbe:
        needs = self.expectation == ProbeExpectation.STDOUT_EQUALS
        if needs and not self.expected_stdout:
            raise ValueError(
                f"probe {self.probe_id!r} compares stdout but declares none"
            )
        if not needs and self.expected_stdout is not None:
            raise ValueError(
                f"probe {self.probe_id!r} declares stdout it will never compare"
            )
        return self


class ProbeResult(StrictModel):
    probe_id: str = Field(min_length=1)
    status: ProbeStatus = ProbeStatus.NOT_RUN
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    detail: str = ""


class ContainmentReport(StrictModel):
    schema_version: str = "1.0"
    workcell_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    results: list[ProbeResult] = Field(default_factory=list)
    contained: bool = False
    breaches: list[str] = Field(default_factory=list)
    unproven: list[str] = Field(default_factory=list)
    detail: str = ""


def _probe(
    probe_id: str,
    category: ContainmentCategory,
    argv: list[str],
    expectation: ProbeExpectation,
    breach_meaning: str,
    expected_stdout: str | None = None,
) -> ContainmentProbe:
    return ContainmentProbe(
        probe_id=probe_id,
        category=category,
        argv=argv,
        expectation=expectation,
        expected_stdout=expected_stdout,
        breach_meaning=breach_meaning,
    )


#: The probes every workcell must pass before it is allowed to run a model.
#:
#: `test -e` exits non-zero when a path is absent, so `PATH_ABSENT` probes are
#: expressed as `COMMAND_REFUSED` against `test -e`; the distinct expectation
#: values exist so the failure message can say which kind of breach occurred.
DEFAULT_CONTAINMENT_PROBES: tuple[ContainmentProbe, ...] = (
    # --- Host filesystem -------------------------------------------------
    _probe(
        "host-windows-users-absent",
        ContainmentCategory.HOST_FILESYSTEM,
        ["test", "-e", "/host_mnt/c/Users"],
        ProbeExpectation.COMMAND_REFUSED,
        "the owner's Windows home directory is visible inside the workcell",
    ),
    _probe(
        "host-root-mount-absent",
        ContainmentCategory.HOST_FILESYSTEM,
        ["test", "-e", "/host"],
        ProbeExpectation.COMMAND_REFUSED,
        "a general host mount exists; the only mounts may be the disposable "
        "clone and the read-only task artifact",
    ),
    _probe(
        "apoapsis-checkout-absent",
        ContainmentCategory.HOST_FILESYSTEM,
        ["test", "-e", "/workspace/.apoapsis"],
        ProbeExpectation.COMMAND_REFUSED,
        "Apoapsis's own state is inside the workspace the model can rewrite",
    ),
    _probe(
        "task-artifact-outside-workspace",
        ContainmentCategory.HOST_FILESYSTEM,
        ["test", "-e", "/workspace/task"],
        ProbeExpectation.COMMAND_REFUSED,
        "the approved task document is inside the delivered project tree and "
        "would appear in the computed delta",
    ),
    _probe(
        "task-artifact-read-only",
        ContainmentCategory.HOST_FILESYSTEM,
        ["sh", "-c", "printf x >> /task/task.md"],
        ProbeExpectation.COMMAND_REFUSED,
        "the model can rewrite its own approved task",
    ),
    # --- Credentials -----------------------------------------------------
    _probe(
        "ssh-agent-absent",
        ContainmentCategory.CREDENTIALS,
        ["sh", "-c", 'test -n "$SSH_AUTH_SOCK"'],
        ProbeExpectation.COMMAND_REFUSED,
        "the owner's SSH agent is forwarded into the workcell",
    ),
    _probe(
        "home-credentials-absent",
        ContainmentCategory.CREDENTIALS,
        ["sh", "-c", "test -e ~/.ssh -o -e ~/.aws -o -e ~/.netrc -o -e ~/.npmrc"],
        ProbeExpectation.COMMAND_REFUSED,
        "owner credentials or package tokens are readable",
    ),
    _probe(
        "no-token-environment",
        ContainmentCategory.CREDENTIALS,
        [
            "sh",
            "-c",
            "env | grep -Eiq '(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL)='",
        ],
        ProbeExpectation.COMMAND_REFUSED,
        "a secret was passed into the workcell environment",
    ),
    _probe(
        "git-remote-sanitized",
        ContainmentCategory.CREDENTIALS,
        ["sh", "-c", "cd /workspace && git remote | grep -q ."],
        ProbeExpectation.COMMAND_REFUSED,
        "the sacrificial clone still has a remote; Git is only safe here "
        "because there is nowhere to push",
    ),
    # --- Network ---------------------------------------------------------
    _probe(
        "no-external-route",
        ContainmentCategory.NETWORK,
        ["sh", "-c", "getent hosts github.com"],
        ProbeExpectation.COMMAND_REFUSED,
        "the workcell can resolve external names, so it has DNS and probably "
        "egress",
    ),
    _probe(
        "no-default-route",
        ContainmentCategory.NETWORK,
        ["sh", "-c", "ip route | grep -q default"],
        ProbeExpectation.COMMAND_REFUSED,
        "the network namespace has a default route; it must have only loopback",
    ),
    _probe(
        "cloud-metadata-unreachable",
        ContainmentCategory.NETWORK,
        ["sh", "-c", "timeout 2 sh -c '</dev/tcp/169.254.169.254/80'"],
        ProbeExpectation.COMMAND_REFUSED,
        "the cloud instance metadata endpoint is reachable",
    ),
    _probe(
        "host-loopback-unreachable",
        ContainmentCategory.NETWORK,
        ["sh", "-c", "timeout 2 sh -c '</dev/tcp/host.docker.internal/80'"],
        ProbeExpectation.COMMAND_REFUSED,
        "the host's own services are reachable from the workcell",
    ),
    # --- Container control ----------------------------------------------
    _probe(
        "docker-socket-absent",
        ContainmentCategory.CONTAINER_CONTROL,
        ["test", "-e", "/var/run/docker.sock"],
        ProbeExpectation.COMMAND_REFUSED,
        "the Docker control socket is mounted; this is full host takeover",
    ),
    _probe(
        "podman-socket-absent",
        ContainmentCategory.CONTAINER_CONTROL,
        ["sh", "-c", "ls /run/podman/podman.sock /run/user/*/podman/podman.sock"],
        ProbeExpectation.COMMAND_REFUSED,
        "a Podman control socket is reachable",
    ),
    _probe(
        "wsl-interop-absent",
        ContainmentCategory.CONTAINER_CONTROL,
        ["sh", "-c", 'test -n "$WSL_INTEROP" -o -e /run/WSL'],
        ProbeExpectation.COMMAND_REFUSED,
        "WSL interop is available, which can launch Windows host processes",
    ),
    _probe(
        "docker-cli-unusable",
        ContainmentCategory.CONTAINER_CONTROL,
        ["sh", "-c", "docker ps"],
        ProbeExpectation.COMMAND_REFUSED,
        "a working Docker client exists inside the workcell",
    ),
    # --- Controller authority --------------------------------------------
    _probe(
        "model-socket-is-only-egress",
        ContainmentCategory.CONTROLLER_AUTHORITY,
        [
            "sh",
            "-c",
            "ss -H -tan 2>/dev/null | grep -v '127.0.0.1' | grep -q LISTEN",
        ],
        ProbeExpectation.COMMAND_REFUSED,
        "something is listening on a non-loopback address, so the "
        "controller-owned socket is not the only way out",
    ),
    _probe(
        "audit-log-unreachable",
        ContainmentCategory.CONTROLLER_AUTHORITY,
        ["sh", "-c", "find / -xdev -name 'audit*.jsonl' -print -quit | grep -q ."],
        ProbeExpectation.COMMAND_REFUSED,
        "the model can see, and therefore might rewrite, its own audit trail",
    ),
    # --- Privilege and ceilings -------------------------------------------
    _probe(
        "non-root-execution",
        ContainmentCategory.PRIVILEGE,
        ["id", "-u"],
        ProbeExpectation.STDOUT_EQUALS,
        "the workcell runs as root",
        expected_stdout="65532",
    ),
    _probe(
        "no-new-privileges",
        ContainmentCategory.PRIVILEGE,
        ["grep", "-q", "NoNewPrivs:\t1", "/proc/self/status"],
        ProbeExpectation.COMMAND_SUCCEEDS,
        "privilege escalation via setuid binaries is not blocked",
    ),
    # The one probe that fails when the box is too tight rather than too
    # loose. A workcell that cannot be written to has perfect containment and
    # no capability, which is the regression this whole slice exists to avoid.
    _probe(
        "workspace-writable",
        ContainmentCategory.RESOURCE_CEILING,
        [
            "sh",
            "-c",
            "touch /workspace/.apoapsis-probe && rm /workspace/.apoapsis-probe",
        ],
        ProbeExpectation.COMMAND_SUCCEEDS,
        "the workspace is not writable, so the agent has no baseline editing "
        "capability at all",
    ),
)


def classify_probe(
    probe: ContainmentProbe,
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
) -> ProbeResult:
    """Turn one probe execution into a status.

    `exit_code is None` means the probe did not complete — a timeout, a runtime
    error, a container that was already gone. That is `INCONCLUSIVE`, not a
    pass, because the forbidden thing was never actually tested.
    """

    if exit_code is None:
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.INCONCLUSIVE,
            stdout=stdout,
            stderr=stderr,
            detail="the probe did not complete, so containment was not tested",
        )

    if probe.expectation == ProbeExpectation.STDOUT_EQUALS:
        if exit_code != 0:
            return ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.INCONCLUSIVE,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                detail=(
                    "the probe was expected to succeed and report a value, and "
                    "instead failed; nothing was established either way"
                ),
            )
        observed = stdout.strip()
        expected = (probe.expected_stdout or "").strip()
        if observed == expected:
            return ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.CONTAINED,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.BREACHED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            detail=(
                f"expected {expected!r} and observed {observed!r}: "
                f"{probe.breach_meaning}"
            ),
        )

    if probe.expectation == ProbeExpectation.COMMAND_SUCCEEDS:
        if exit_code == 0:
            return ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.CONTAINED,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.BREACHED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            detail=probe.breach_meaning,
        )

    # Every remaining expectation is satisfied by the command being refused.
    if exit_code != 0:
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.CONTAINED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
    return ProbeResult(
        probe_id=probe.probe_id,
        status=ProbeStatus.BREACHED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        detail=probe.breach_meaning,
    )


def evaluate_containment(
    results: list[ProbeResult],
    *,
    workcell_manifest_digest: str,
    probes: tuple[ContainmentProbe, ...] = DEFAULT_CONTAINMENT_PROBES,
) -> ContainmentReport:
    """Fail closed. Containment holds only if every probe says so.

    A probe that is missing from `results` entirely is treated as `NOT_RUN`,
    because a suite that quietly shrank is the failure mode most likely to
    survive review.
    """

    by_id = {item.probe_id: item for item in results}
    complete: list[ProbeResult] = []
    for probe in probes:
        complete.append(
            by_id.get(probe.probe_id)
            or ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.NOT_RUN,
                detail="no result was recorded for this probe",
            )
        )

    breaches = [
        item.probe_id for item in complete if item.status == ProbeStatus.BREACHED
    ]
    unproven = [
        item.probe_id
        for item in complete
        if item.status in {ProbeStatus.INCONCLUSIVE, ProbeStatus.NOT_RUN}
    ]
    unexpected = sorted(set(by_id) - {probe.probe_id for probe in probes})

    if breaches:
        detail = f"{len(breaches)} containment breach(es): " + ", ".join(breaches)
    elif unproven:
        detail = (
            f"{len(unproven)} probe(s) did not establish containment: "
            + ", ".join(unproven)
            + ". An unproven boundary is not a closed one."
        )
    else:
        detail = f"all {len(complete)} probes observed the boundary holding"
    if unexpected:
        detail += (
            f" Results were also supplied for {len(unexpected)} probe(s) not in "
            f"the suite: {', '.join(unexpected)}."
        )

    return ContainmentReport(
        workcell_manifest_digest=workcell_manifest_digest,
        results=complete,
        contained=not breaches and not unproven,
        breaches=breaches,
        unproven=unproven,
        detail=detail,
    )

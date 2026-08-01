"""Prove the agent's tools work, by making them do something.

A realised tool list is a claim about *registration*. Slice 2C's arms would
have failed that check, which is why it is in the profile gate — but passing it
would still not prove the tools function. A `write_file` that is registered and
then refused by a read-only filesystem, a `run_shell_command` with no shell in
the image, an `edit` that cannot resolve its target: all present, all useless.

So this exercises them for real, in the sacrificial clone, before any measured
task begins. Read a file the controller planted, edit it, run a shell command
that observes the edit, and confirm the controller can see the result from
outside the container.

Two properties are deliberate:

**The exercise is driven by the controller, not by the model.** These are
`docker exec` operations against the same workspace the agent will use, so a
failure means the environment is wrong rather than that the model chose badly.
Asking a model to demonstrate its tools would measure the model.

**It cleans up after itself and says whether it managed to.** A probe artifact
left in the clone would show up in the computed delta as work the agent did not
do.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: Lives at the clone root, prefixed so an admission diff that ever sees it can
#: name it immediately as harness residue rather than agent work.
PROBE_FILENAME = ".apoapsis-capability-probe"
PROBE_INITIAL = "apoapsis-readiness-initial\n"
PROBE_EDITED = "apoapsis-readiness-edited\n"


class ReadinessOperation(StrEnum):
    WORKSPACE_WRITABLE = "workspace_writable"
    READ = "read"
    EDIT = "edit"
    SHELL = "shell"
    #: The controller sees the edit from outside, so the workspace really is
    #: the shared one and not a copy inside the container.
    HOST_VISIBLE = "host_visible"
    CLEANUP = "cleanup"


class ReadinessStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ReadinessOperationResult(StrictModel):
    operation: ReadinessOperation
    status: ReadinessStatus = ReadinessStatus.NOT_RUN
    exit_code: int | None = None
    detail: str = ""


class CapabilityReadinessReport(StrictModel):
    schema_version: str = "1.0"
    results: list[ReadinessOperationResult] = Field(default_factory=list)
    ready: bool = False
    #: True only when the probe artifact is gone from the clone afterwards.
    residue_free: bool = False
    detail: str = Field(min_length=1)

    def result(self, operation: ReadinessOperation) -> ReadinessOperationResult:
        for item in self.results:
            if item.operation == operation:
                return item
        raise KeyError(operation)


def _classify(
    operation: ReadinessOperation,
    *,
    exit_code: int | None,
    stdout: str = "",
    expect: str | None = None,
    failure_detail: str,
) -> ReadinessOperationResult:
    if exit_code is None:
        return ReadinessOperationResult(
            operation=operation,
            status=ReadinessStatus.FAILED,
            detail=f"{failure_detail}: the operation did not complete",
        )
    if exit_code != 0:
        return ReadinessOperationResult(
            operation=operation,
            status=ReadinessStatus.FAILED,
            exit_code=exit_code,
            detail=f"{failure_detail}: exit {exit_code}",
        )
    if expect is not None and stdout.strip() != expect.strip():
        return ReadinessOperationResult(
            operation=operation,
            status=ReadinessStatus.FAILED,
            exit_code=exit_code,
            detail=(
                f"{failure_detail}: expected {expect.strip()!r}, observed "
                f"{stdout.strip()[:120]!r}"
            ),
        )
    return ReadinessOperationResult(
        operation=operation, status=ReadinessStatus.PASSED, exit_code=exit_code
    )


def run_capability_readiness(
    exec_fn,
    *,
    workspace_container_path: str = "/workspace",
    workspace_host_path: str | None = None,
) -> CapabilityReadinessReport:
    """Exercise read, edit, and shell in the clone. Never calls a model.

    `exec_fn(argv, timeout_seconds) -> (exit_code, stdout, stderr)`, which is
    `LiveWorkcellSession.exec`.

    `workspace_host_path` enables the host-visibility check: the controller
    reads the edited file from outside the container, which proves the agent
    and the delta-admission step are looking at the same bytes. Without it that
    operation stays `NOT_RUN`, and `NOT_RUN` is not a pass.
    """

    probe = f"{workspace_container_path.rstrip('/')}/{PROBE_FILENAME}"
    results: list[ReadinessOperationResult] = []

    def run(argv: list[str]) -> tuple[int | None, str, str]:
        return exec_fn(argv, 60.0)

    # 1. The clone must be writable at all. Checked first because every later
    #    failure would otherwise be reported as a tool problem.
    code, out, err = run(
        ["sh", "-c", f"printf '%s' '{PROBE_INITIAL.strip()}' > {probe} && echo ok"]
    )
    results.append(
        _classify(
            ReadinessOperation.WORKSPACE_WRITABLE,
            exit_code=code,
            stdout=out,
            expect="ok",
            failure_detail=(
                "the sacrificial clone is not writable, so no editing tool "
                "could work regardless of registration"
            ),
        )
    )
    if results[-1].status != ReadinessStatus.PASSED:
        return _finish(results, residue_free=False)

    # 2. Read it back.
    code, out, err = run(["cat", probe])
    results.append(
        _classify(
            ReadinessOperation.READ,
            exit_code=code,
            stdout=out,
            expect=PROBE_INITIAL.strip(),
            failure_detail="the planted file could not be read back",
        )
    )

    # 3. Edit in place -- a substitution, not a rewrite, because a whole-file
    #    write would not distinguish "can create" from "can modify".
    code, out, err = run(
        [
            "sh",
            "-c",
            f"sed -i 's/initial/edited/' {probe} && cat {probe}",
        ]
    )
    results.append(
        _classify(
            ReadinessOperation.EDIT,
            exit_code=code,
            stdout=out,
            expect=PROBE_EDITED.strip(),
            failure_detail="an in-place edit of the planted file failed",
        )
    )

    # 4. Shell: run a command that observes the edited state, so a shell that
    #    runs but cannot see the workspace is caught too.
    code, out, err = run(
        ["sh", "-c", f"grep -c edited {probe}"]
    )
    results.append(
        _classify(
            ReadinessOperation.SHELL,
            exit_code=code,
            stdout=out,
            expect="1",
            failure_detail="a shell command could not observe the edited file",
        )
    )

    # 5. The controller reads the same file from outside the container. If the
    #    agent's edits are not visible here, delta admission would compute its
    #    diff against bytes the agent never touched.
    if workspace_host_path is not None:
        from pathlib import Path

        host_probe = Path(workspace_host_path) / PROBE_FILENAME
        try:
            observed = host_probe.read_text(encoding="utf-8")
            results.append(
                _classify(
                    ReadinessOperation.HOST_VISIBLE,
                    exit_code=0,
                    stdout=observed,
                    expect=PROBE_EDITED.strip(),
                    failure_detail=(
                        "the controller sees different bytes than the workcell, "
                        "so the workspace is not genuinely shared"
                    ),
                )
            )
        except OSError as exc:
            results.append(
                ReadinessOperationResult(
                    operation=ReadinessOperation.HOST_VISIBLE,
                    status=ReadinessStatus.FAILED,
                    detail=(
                        "the controller could not read the probe file from the "
                        f"host side of the mount: {exc}"
                    ),
                )
            )

    # 6. Cleanup, and confirm it worked. Residue would enter the delta.
    code, out, err = run(
        ["sh", "-c", f"rm -f {probe} && test ! -e {probe} && echo gone"]
    )
    cleanup = _classify(
        ReadinessOperation.CLEANUP,
        exit_code=code,
        stdout=out,
        expect="gone",
        failure_detail="the probe artifact could not be removed from the clone",
    )
    results.append(cleanup)
    return _finish(results, residue_free=cleanup.status == ReadinessStatus.PASSED)


def _finish(
    results: list[ReadinessOperationResult], *, residue_free: bool
) -> CapabilityReadinessReport:
    """Fail closed: an operation with no result is `NOT_RUN`, which is not a pass."""

    by_operation = {item.operation: item for item in results}
    complete = [
        by_operation.get(operation)
        or ReadinessOperationResult(
            operation=operation,
            status=ReadinessStatus.NOT_RUN,
            detail="this operation was never attempted",
        )
        for operation in ReadinessOperation
    ]
    failed = [item for item in complete if item.status == ReadinessStatus.FAILED]
    not_run = [item for item in complete if item.status == ReadinessStatus.NOT_RUN]

    if failed:
        detail = (
            f"{len(failed)} readiness operation(s) failed: "
            + "; ".join(f"{item.operation.value}: {item.detail}" for item in failed)
        )
    elif not_run:
        detail = (
            f"{len(not_run)} readiness operation(s) never ran: "
            + ", ".join(item.operation.value for item in not_run)
            + ". An unattempted operation is not a demonstrated capability."
        )
    else:
        detail = (
            "read, in-place edit, and shell all executed against the sacrificial "
            "clone, and the probe artifact was removed afterwards"
        )
    return CapabilityReadinessReport(
        results=complete,
        ready=not failed and not not_run and residue_free,
        residue_free=residue_free,
        detail=detail,
    )

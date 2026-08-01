"""Fast advisory feedback for the agent. Never evidence of completion.

SWE-agent found that immediate syntax feedback at edit time materially helps a
model fix its own mistakes, and Qwen Code ships an LSP surface. Both are worth
having. Both are also exactly the shape of thing that ended Crisis Atlas Slice 2
early: a cheap green signal, arriving before the work was done, treated as
though it meant the work was done.

So this module is built around one asymmetry:

    A diagnostic that finds a problem is useful to the agent.
    A diagnostic that finds nothing proves nothing at all.

`DiagnosticStatus` therefore never collapses to a boolean. A language server
that is missing, misconfigured, crashed, or timed out yields `NOT_CHECKED`, and
`NOT_CHECKED` is not `CLEAN`. That distinction is the whole ADR 0069 lesson
transposed from verification onto feedback: absence of a reading reported as
absence of the thing is the failure this codebase treats as worse than a bug.

**Structural guarantee.** `evaluate_checkpoint` takes an admission result and a
readiness report and nothing else; a test asserts its signature. A
`DiagnosticReport` is deliberately *not* a `StructuredWitness`, so it cannot
satisfy a contract obligation even by accident, and there is no code path that
converts one into the other. The controller records diagnostics as evidence
because an audit trail wants them. Readiness never sees them.

`advisory` is `Literal[True]`. It is not a flag anybody can set to `False` in a
config file six months from now.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Literal, Sequence

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: Diagnostics are bounded like every other tool observation. A language server
#: on a large repository can emit thousands of findings, and a wall of them is
#: less useful to the agent than the first few -- SWE-agent's ACI work found
#: concise observations were less confusing than exhaustive ones.
MAX_REPORTED_DIAGNOSTICS = 50


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    HINT = "hint"


class DiagnosticStatus(str, Enum):
    """What the diagnostic pass actually established.

    Four values rather than a boolean, because three different things all look
    like "no errors" and only one of them is one.
    """

    #: The tool ran and reported findings.
    FINDINGS = "findings"
    #: The tool ran, completed, and reported nothing. Advisory even so: a clean
    #: parse is not a working feature.
    CLEAN = "clean"
    #: No tool was configured or present. **Not** clean.
    TOOL_ABSENT = "tool_absent"
    #: A tool was present and did not complete -- crash, timeout, bad output.
    #: **Not** clean.
    TOOL_FAILED = "tool_failed"

    @property
    def is_a_reading(self) -> bool:
        """Whether anything was actually observed.

        The property callers should use instead of testing for an empty
        findings list, which is true for all four values.
        """

        return self in (DiagnosticStatus.FINDINGS, DiagnosticStatus.CLEAN)


class Diagnostic(StrictModel):
    """One finding, in the agent's terms."""

    path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    severity: DiagnosticSeverity
    code: str | None = None
    message: str = Field(min_length=1)
    #: Which tool said so. An agent weighing a finding needs to know whether a
    #: parser or a linter produced it.
    tool: str = Field(min_length=1)


class DiagnosticReport(StrictModel):
    """A diagnostic pass over the workcell, recorded as controller evidence.

    Deliberately **not** a `StructuredWitness`. A witness discharges a contract
    obligation; this cannot, and keeping them different types means no future
    caller can pass one where the other is expected. The separation is the
    guarantee -- not a naming convention.
    """

    schema_version: str = "1.0"
    #: Fixed by the type system. Diagnostics are advisory in every code path
    #: that exists and in every code path that can be added.
    advisory: Literal[True] = True
    status: DiagnosticStatus
    tool_name: str = Field(min_length=1)
    #: Version or digest of whatever ran, so a finding is attributable to a
    #: build. `None` when nothing ran.
    tool_version: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    #: Present when the pass produced more than it reported.
    truncated_count: int = Field(default=0, ge=0)
    #: The worktree the pass observed. A diagnostic bound to a different tree is
    #: a stale reading, and the agent should be able to tell.
    worktree_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    #: Why nothing was observed, when nothing was. Required for the two
    #: non-reading statuses so a silent pass is never indistinguishable from a
    #: clean one in the audit trail.
    reason: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)

    @property
    def errors(self) -> list[Diagnostic]:
        return [
            item
            for item in self.diagnostics
            if item.severity is DiagnosticSeverity.ERROR
        ]

    def agent_summary(self) -> str:
        """What the agent is told. Explicit about not having checked.

        The wording matters more than it looks. "No problems found" after a
        crashed language server is the sentence that ends a slice early, so the
        two non-reading statuses say plainly that nothing was checked and that
        this is not an all-clear.
        """

        if self.status is DiagnosticStatus.TOOL_ABSENT:
            return (
                f"NOT CHECKED: no {self.tool_name} available in this workcell "
                f"({self.reason or 'no tool present'}). This is not an "
                "all-clear; nothing was inspected."
            )
        if self.status is DiagnosticStatus.TOOL_FAILED:
            return (
                f"NOT CHECKED: {self.tool_name} did not complete "
                f"({self.reason or 'no detail'}). This is not an all-clear; "
                "nothing was inspected."
            )
        if self.status is DiagnosticStatus.CLEAN:
            return (
                f"{self.tool_name} found no problems. Advisory only: this says "
                "the code parses, not that the slice is implemented."
            )
        shown = "\n".join(
            f"  {item.path}"
            f"{':' + str(item.line) if item.line else ''}"
            f" [{item.severity.value}] {item.message}"
            for item in self.diagnostics[:MAX_REPORTED_DIAGNOSTICS]
        )
        more = (
            f"\n  ... and {self.truncated_count} more"
            if self.truncated_count
            else ""
        )
        return f"{self.tool_name} findings (advisory):\n{shown}{more}"


#: Runs one diagnostic tool over a root and returns `(exit_code, stdout,
#: stderr)`. Injected so the controller owns process execution and the tests do
#: not need a container.
DiagnosticRunner = Callable[[Sequence[str], float], tuple[int, str, str]]


def not_checked(
    tool_name: str, reason: str, *, failed: bool = False
) -> DiagnosticReport:
    """The report for a pass that did not happen.

    A named constructor rather than an inline literal, because "what do we
    return when the tool is missing" is precisely the decision that should have
    exactly one answer in the codebase.
    """

    return DiagnosticReport(
        status=(
            DiagnosticStatus.TOOL_FAILED if failed else DiagnosticStatus.TOOL_ABSENT
        ),
        tool_name=tool_name,
        reason=reason,
    )


def run_syntax_diagnostics(
    *,
    paths: Sequence[str],
    runner: DiagnosticRunner,
    worktree_fingerprint: str | None = None,
    timeout_seconds: float = 60.0,
) -> DiagnosticReport:
    """Parse-check changed Python files inside the workcell.

    Syntax only, deliberately. It is the cheapest useful signal, it has no
    project configuration to get wrong, and it cannot be confused for a test.
    A richer language-server pass belongs behind the same `DiagnosticReport`
    contract and the same `NOT_CHECKED` discipline; nothing here needs to change
    to add one.
    """

    targets = [path for path in paths if path.endswith(".py")]
    if not targets:
        return DiagnosticReport(
            status=DiagnosticStatus.CLEAN,
            tool_name="python-compile",
            diagnostics=[],
            worktree_fingerprint=worktree_fingerprint,
            reason="no Python files in the change set",
        )
    try:
        code, stdout, stderr = runner(
            ["python3", "-m", "py_compile", *targets], timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 -- becomes NOT_CHECKED, never clean
        return not_checked(
            "python-compile", f"{type(exc).__name__}: {exc}", failed=True
        )

    if code == 0:
        return DiagnosticReport(
            status=DiagnosticStatus.CLEAN,
            tool_name="python-compile",
            worktree_fingerprint=worktree_fingerprint,
        )
    findings = _parse_py_compile(stderr or stdout)
    if not findings:
        # A non-zero exit whose output could not be parsed is a failed pass, not
        # a clean one and not an empty finding list. Returning CLEAN here would
        # reintroduce the exact defect this module exists to prevent.
        return not_checked(
            "python-compile",
            f"exit {code} with unparseable output: {(stderr or stdout)[:200]!r}",
            failed=True,
        )
    return DiagnosticReport(
        status=DiagnosticStatus.FINDINGS,
        tool_name="python-compile",
        diagnostics=findings[:MAX_REPORTED_DIAGNOSTICS],
        truncated_count=max(0, len(findings) - MAX_REPORTED_DIAGNOSTICS),
        worktree_fingerprint=worktree_fingerprint,
    )


def _looks_like_an_exception(line: str) -> bool:
    """Whether a traceback line is the diagnosis rather than echoed source.

    Deliberately narrow: `Name: detail` where the name ends in Error, Warning,
    or Exception. A looser rule matches ordinary annotated Python source such as
    `x: int = 1` and would report it as the finding.
    """

    head, separator, _ = line.partition(":")
    if not separator or " " in head.strip():
        return False
    return head.strip().endswith(("Error", "Warning", "Exception"))


def _parse_py_compile(text: str) -> list[Diagnostic]:
    """Pull file/line/message out of a `py_compile` traceback.

    Tolerant by design: an unrecognised format yields no findings, and the
    caller turns that into `TOOL_FAILED`. Guessing a location would produce a
    diagnostic pointing at the wrong line, which is worse than none.
    """

    findings: list[Diagnostic] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith('File "'):
            continue
        try:
            path = stripped.split('"')[1]
        except IndexError:
            continue
        number = None
        if ", line " in stripped:
            tail = stripped.split(", line ")[1].split(",")[0].strip()
            number = int(tail) if tail.isdigit() else None
        # Prefer the exception line over the echoed source line. `py_compile`
        # prints the offending source before the diagnosis, so taking the first
        # non-marker line yields `def handler(` -- a location with no finding
        # attached, which tells the agent nothing it did not already know.
        window: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip().startswith('File "'):
                break
            window.append(following.strip())
        message = ""
        for candidate in window:
            if _looks_like_an_exception(candidate):
                message = candidate
                break
        if not message:
            message = next(
                (
                    candidate
                    for candidate in window
                    if candidate and not candidate.startswith(("^", "~", "|"))
                ),
                "",
            )
        if message:
            findings.append(
                Diagnostic(
                    path=path,
                    line=number,
                    severity=DiagnosticSeverity.ERROR,
                    message=message,
                    tool="python-compile",
                )
            )
    return findings

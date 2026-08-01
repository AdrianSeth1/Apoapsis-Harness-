"""Controller-owned witness emitters. The model never authors evidence.

Slice 4 defined what a witness must contain and refused witnesses that failed
to contain it. Nothing produced one, which made the rule enforceable in
principle and unenforced in practice. These are the producers.

The governing rule, stated once so every emitter can be read against it:

> Coverage is derived from an artifact **the controller produced and hashed**.
> A coverage claim that arrived as text is never accepted.

That is not paranoia about a lying model so much as about an ambiguous one. A
model that says "I ran the tests and they covered the new service" may be
right, wrong, or describing a different run; there is no way to tell. A
`coverage.json` the controller told `coverage.py` to write, then read and
hashed itself, has no such ambiguity — and `CoverageObservation
.source_artifact_sha256` records which file the numbers came from.

Every emitter fails closed. If a command cannot be run, or an artifact cannot
be parsed, the emitter raises rather than returning a witness with an empty
section, because an empty section is indistinguishable from a section that
found nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Protocol

from apoapsis.workcell.witness import (
    CoverageObservation,
    EvidenceClass,
    HttpExchange,
    ProcessObservation,
    StructuredWitness,
    WitnessKind,
)


class EmitterError(RuntimeError):
    """A witness could not be produced, so none is produced."""


class CommandRunner(Protocol):
    """How the controller runs a command. Never a shell string."""

    def __call__(
        self, argv: list[str], *, timeout_seconds: float
    ) -> tuple[int | None, str, str]: ...


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _witness_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def parse_coverage_json(
    payload: dict, *, source_sha256: str, collection_method: str
) -> CoverageObservation:
    """Read `coverage.py`'s JSON report into a coverage observation.

    Only `executed_lines` is taken. `coverage.py` also reports summaries and
    percentages; a percentage cannot answer "was this function reached", and
    reading one would invite exactly the file-level reasoning that missed
    Crisis Atlas Slice 3.
    """

    # `observed_symbols` is read from the same artifact as the line data, so an
    # interface obligation is discharged by measurement rather than by the
    # planner's suggestion that a symbol ought to exist. `coverage.py` does not
    # emit it natively; a wrapper that can report executed function names puts
    # them here, and one that cannot leaves it empty rather than guessing.
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise EmitterError(
            "the coverage report names no files; a report that measured "
            "nothing is not evidence that nothing needed measuring"
        )
    executed_lines: dict[str, list[int]] = {}
    observed_symbols: set[str] = set()
    for raw_path, record in files.items():
        if not isinstance(record, dict):
            continue
        lines = record.get("executed_lines")
        if not isinstance(lines, list):
            continue
        normalised = str(raw_path).replace("\\", "/").removeprefix("./")
        executed_lines[normalised] = sorted(
            int(item) for item in lines if isinstance(item, int)
        )
        for key in ("executed_functions", "executed_classes"):
            reported = record.get(key)
            if isinstance(reported, list):
                observed_symbols.update(
                    str(item) for item in reported if isinstance(item, str)
                )
    if not executed_lines:
        raise EmitterError(
            "the coverage report contained no executed lines for any file"
        )
    return CoverageObservation(
        executed_paths=sorted(executed_lines),
        executed_lines=executed_lines,
        observed_symbols=sorted(observed_symbols),
        collection_method=collection_method,
        source_artifact_sha256=source_sha256,
    )


def emit_test_witness(
    runner: CommandRunner,
    *,
    command_name: str,
    command_version: str,
    argv: list[str],
    worktree_fingerprint: str,
    coverage_artifact: Path,
    candidate_commit: str | None = None,
    criteria_proved: list[str] | None = None,
    evidence_class: EvidenceClass = EvidenceClass.INDEPENDENT,
    timeout_seconds: float = 600.0,
    collection_method: str = "coverage.py json report",
) -> StructuredWitness:
    """Run a test command under coverage and witness what it actually reached.

    `coverage_artifact` is where the controller told the run to write its JSON
    report. It is deleted before the run, so a stale report from a previous
    attempt cannot be read as this run's evidence — the same staleness problem
    the fingerprint solves at the witness level, one layer down.
    """

    artifact = Path(coverage_artifact)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.unlink(missing_ok=True)

    started = time.monotonic()
    exit_code, stdout, stderr = runner(argv, timeout_seconds=timeout_seconds)
    duration = time.monotonic() - started

    if not artifact.is_file():
        raise EmitterError(
            f"{command_name!r} produced no coverage artifact at {artifact}. "
            "Without it there is no way to tell which paths the run reached, "
            "and a witness that asserted coverage anyway would be a claim."
        )
    source_sha = _sha256_file(artifact)
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmitterError(f"the coverage artifact could not be read: {exc}") from exc

    coverage = parse_coverage_json(
        payload, source_sha256=source_sha, collection_method=collection_method
    )
    passed = exit_code == 0
    return StructuredWitness(
        witness_id=_witness_id("tests"),
        kind=WitnessKind.TEST_SUITE,
        evidence_class=evidence_class,
        command_name=command_name,
        command_version=command_version,
        command_argv=list(argv),
        candidate_commit=candidate_commit,
        worktree_fingerprint=worktree_fingerprint,
        passed=passed,
        duration_seconds=duration,
        coverage=coverage,
        # A failing run proves nothing, so it claims nothing. `validate_witness`
        # refuses a failing witness that lists criteria, and this is the
        # producer side of that rule.
        criteria_proved=list(criteria_proved or []) if passed else [],
        artifact_sha256={str(artifact.name): source_sha},
        detail=(stdout[-800:] + stderr[-800:]).strip(),
    )


class LaunchProbe(Protocol):
    """Drives one HTTP exchange against the launched process."""

    def __call__(
        self, method: str, route: str
    ) -> tuple[int, list[str]]: ...


def emit_launch_witness(
    *,
    command_name: str,
    command_version: str,
    argv: list[str],
    worktree_fingerprint: str,
    start_process: Callable[[], ProcessObservation],
    probe: LaunchProbe,
    exchanges: list[tuple[str, str, bool]],
    stop_process: Callable[[], bool],
    candidate_commit: str | None = None,
    criteria_proved: list[str] | None = None,
    coverage: CoverageObservation | None = None,
) -> StructuredWitness:
    """Launch a real process, drive real routes, and record what happened.

    `exchanges` is a list of `(method, route, mutating)`. The caller is
    expected to follow every mutating call with a read of the same route --
    `validate_witness` refuses a witness where it did not, because a POST
    nobody read back proves the endpoint accepted a request and not that
    anything persisted.

    `stop_process` always runs, including on failure. A server left behind can
    make a *later* witness pass for the wrong reason, which is worse than the
    failure that leaked it.
    """

    started = time.monotonic()
    try:
        process = start_process()
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        raise EmitterError(f"{command_name!r} could not start its process: {exc}") from exc

    recorded: list[HttpExchange] = []
    failure: str | None = None
    try:
        for method, route, mutating in exchanges:
            status, assertions = probe(method, route)
            recorded.append(
                HttpExchange(
                    method=method,
                    route=route,
                    status=status,
                    assertions=assertions,
                    mutating=mutating,
                )
            )
    except Exception as exc:  # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        cleaned = False
        try:
            cleaned = bool(stop_process())
        except Exception as exc:  # noqa: BLE001
            failure = failure or f"cleanup failed: {exc}"
        process = process.model_copy(update={"cleaned_up": cleaned})

    if not recorded:
        raise EmitterError(
            f"{command_name!r} launched a process and exercised no route; a "
            "process that starts and is never called proves only that it starts"
        )

    passed = failure is None and all(item.status < 400 for item in recorded)
    return StructuredWitness(
        witness_id=_witness_id("launch"),
        kind=WitnessKind.LAUNCH_HTTP,
        evidence_class=EvidenceClass.INDEPENDENT,
        command_name=command_name,
        command_version=command_version,
        command_argv=list(argv),
        candidate_commit=candidate_commit,
        worktree_fingerprint=worktree_fingerprint,
        passed=passed,
        duration_seconds=time.monotonic() - started,
        process=process,
        exchanges=recorded,
        coverage=coverage,
        criteria_proved=list(criteria_proved or []) if passed else [],
        detail=failure or "",
    )


#: Route literals a launch witness exercised, for matching against the
#: behaviour units a candidate introduced.
def exercised_routes(witness: StructuredWitness) -> set[str]:
    return {item.route for item in witness.exchanges}


_STATUS_ASSERTION = re.compile(r"^status == \d{3}$")


def normalise_assertion(text: str) -> str:
    """Keep assertions comparable across runs.

    A witness whose assertions are free prose cannot be compared with the same
    command's output yesterday. This is a light touch -- whitespace and a
    canonical status form -- rather than a schema, because over-constraining
    would push wrappers into recording nothing.
    """

    collapsed = " ".join(text.split())
    if _STATUS_ASSERTION.match(collapsed):
        return collapsed
    return collapsed

"""Structured witnesses: what a command *proved*, not what it was called.

The Crisis Atlas sentence this module exists to make impossible:

> A command named `behavioral-integration` is not evidence that integration
> occurred.

Slice 4 of that trial had a configured command with a reassuring name, an exit
code of zero, and a green suite. None of it touched the new service. The
harness recorded "behavioural integration passed" because a command called
`behavioral-integration` had exited zero, which is an argument from vocabulary.

A `StructuredWitness` replaces the name with a record of what actually
happened: which process was launched and on which address, which routes were
called with which methods, which assertions held, which mutations were made and
re-read, what was cleaned up, and — the part that makes it a witness rather
than a log — **which criteria it claims to prove and which paths it actually
exercised**.

Two rules run through everything here.

**Fail closed.** A wrapper that cannot produce its declared evidence must not
emit a witness that merely lacks a field. `require_witness` refuses anything
incomplete, because a witness with a missing section is indistinguishable from
one whose section found nothing.

**Bound to a fingerprint.** Every witness names the worktree it observed. A
witness from before the last edit describes code that no longer exists, and
ADR 0072 already established that stale evidence reading as current is how a
superseded pass becomes a claim.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel, utc_now

WITNESS_SCHEMA_VERSION = "1.0"
_SHA256_HEX = r"^[0-9a-f]{64}$"


class WitnessKind(StrEnum):
    #: A test suite run. Proves what its coverage section says it exercised,
    #: and nothing more.
    TEST_SUITE = "test_suite"
    #: A real process launched, bound, and driven over HTTP.
    LAUNCH_HTTP = "launch_http"
    #: The product exercised through its own boundary (browser, CLI, API).
    BEHAVIOURAL = "behavioural"
    #: A static check: linting, type checking, a forbidden-string search.
    #: Deliberately weak — it can refute, but it cannot prove a path is reached.
    STATIC = "static"


class EvidenceClass(StrEnum):
    """Who authored the thing doing the proving.

    The handoff requires this distinction because the unrestricted control
    wrote 87 passing tests and still shipped a broken status filter. Its own
    tests helped it build; they did not independently prove the product.
    """

    #: Owner-authored or harness-owned. The only class that proves alone.
    INDEPENDENT = "independent"
    #: Written by the model in this session.
    MODEL_AUTHORED = "model_authored"
    #: Present in the seed. Green inherited tests are the Crisis Atlas trap:
    #: they stayed green precisely because they never imported the new file.
    INHERITED = "inherited"


class HttpExchange(StrictModel):
    """One request the witness actually made."""

    method: str = Field(min_length=1)
    route: str = Field(min_length=1)
    status: int = Field(ge=100, le=599)
    #: Normalised assertions that held, e.g. "body.items has length 1".
    assertions: list[str] = Field(default_factory=list)
    #: True when this call changed server state, so a later read can prove it.
    mutating: bool = False


class ProcessObservation(StrictModel):
    """The process a launch witness started, and where it really bound."""

    command: list[str] = Field(min_length=1)
    #: How readiness was determined -- a port becoming connectable, a log line.
    #: Named so "we slept three seconds" cannot masquerade as a readiness check.
    readiness_condition: str = Field(min_length=1)
    #: What the process bound, observed rather than requested. A server that
    #: silently fell back to another port is a real defect.
    bound_address: str = Field(min_length=1)
    exit_code: int | None = None
    cleaned_up: bool = False
    cleanup_detail: str = ""


class CoverageObservation(StrictModel):
    """Which repository paths a run actually reached.

    This is what makes the new-component rule enforceable. A test file that
    exists proves nothing; a test run whose coverage names the new module
    proves the new path was reached.
    """

    #: Repository-relative paths executed during the run.
    executed_paths: list[str] = Field(default_factory=list)
    #: Executed line numbers per path. Path granularity cannot answer the
    #: question a *modified* file raises: the file was already covered, and the
    #: new function inside it may not be. Crisis Atlas Slice 3's unreachable
    #: export routes lived in a modified file for exactly this reason.
    executed_lines: dict[str, list[int]] = Field(default_factory=dict)
    #: Modules imported, for languages where import is the meaningful signal.
    imported_modules: list[str] = Field(default_factory=list)
    #: How the coverage was collected. `None` means it was asserted rather
    #: than measured, which `require_witness` refuses.
    collection_method: str | None = None
    #: SHA-256 of the raw artifact the coverage was parsed out of. Present
    #: only when the controller produced and read that file itself; a witness
    #: without it is a coverage *claim*, which the emitters never make.
    source_artifact_sha256: str | None = Field(default=None, pattern=_SHA256_HEX)

    def covers(self, path: str, start_line: int, end_line: int) -> bool:
        """True when any line in `[start_line, end_line]` was executed."""

        executed = set(self.executed_lines.get(path, ()))
        if executed:
            return any(line in executed for line in range(start_line, end_line + 1))
        # No line data for this path: fall back to path granularity, which is
        # all a coverage tool without line reporting can offer.
        return path in set(self.executed_paths)


class StructuredWitness(StrictModel):
    """One versioned, fingerprint-bound record of what a command proved."""

    schema_version: str = WITNESS_SCHEMA_VERSION
    witness_id: str = Field(min_length=1)
    kind: WitnessKind
    evidence_class: EvidenceClass
    #: The configured command's identity. Recorded, never trusted as meaning.
    command_name: str = Field(min_length=1)
    command_version: str = Field(min_length=1)
    command_argv: list[str] = Field(min_length=1)
    #: What this witness observed. Both are required: a witness that does not
    #: name its worktree cannot be checked for staleness.
    candidate_commit: str | None = None
    worktree_fingerprint: str = Field(pattern=_SHA256_HEX)
    passed: bool
    started_at: datetime = Field(default_factory=utc_now)
    duration_seconds: float = Field(default=0.0, ge=0)
    process: ProcessObservation | None = None
    exchanges: list[HttpExchange] = Field(default_factory=list)
    coverage: CoverageObservation | None = None
    #: Criterion identifiers this witness claims to prove. A claim, checked by
    #: the acceptance contract against what the witness actually contains.
    criteria_proved: list[str] = Field(default_factory=list)
    #: Hashes of any artifact the run produced, so a later reader can tell
    #: whether it is looking at the same output.
    artifact_sha256: dict[str, str] = Field(default_factory=dict)
    detail: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> StructuredWitness:
        if self.kind == WitnessKind.LAUNCH_HTTP and self.process is None:
            raise ValueError(
                "a launch witness must record the process it started; without it "
                "there is no evidence anything was launched"
            )
        if self.kind == WitnessKind.LAUNCH_HTTP and not self.exchanges:
            raise ValueError(
                "a launch witness must record the routes it exercised; a process "
                "that started and was never called proves only that it starts"
            )
        return self

    @property
    def exercised_paths(self) -> set[str]:
        return set(self.coverage.executed_paths) if self.coverage else set()

    @property
    def mutations(self) -> list[HttpExchange]:
        return [item for item in self.exchanges if item.mutating]

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WitnessProblem(StrEnum):
    SCHEMA_VERSION_UNKNOWN = "schema_version_unknown"
    STALE_FINGERPRINT = "stale_fingerprint"
    COMMAND_NAME_ONLY = "command_name_only"
    NO_COVERAGE_METHOD = "no_coverage_method"
    LAUNCH_WITHOUT_READINESS = "launch_without_readiness"
    LAUNCH_NOT_CLEANED_UP = "launch_not_cleaned_up"
    MUTATION_NEVER_RE_READ = "mutation_never_re_read"
    CLAIMS_WITHOUT_EVIDENCE = "claims_without_evidence"
    FAILED_WITNESS_CLAIMS_PROOF = "failed_witness_claims_proof"


class WitnessRejection(StrictModel):
    problem: WitnessProblem
    witness_id: str
    detail: str = Field(min_length=1)


class WitnessRefused(RuntimeError):
    """A witness cannot be used as evidence."""


def validate_witness(
    witness: StructuredWitness, *, current_fingerprint: str | None = None
) -> list[WitnessRejection]:
    """Return every reason this witness may not be used. Empty means usable.

    Deliberately returns all problems rather than the first, for the same
    reason admission does: a wrapper author fixing one at a time pays a full
    run per problem.
    """

    problems: list[WitnessRejection] = []

    def reject(problem: WitnessProblem, detail: str) -> None:
        problems.append(
            WitnessRejection(problem=problem, witness_id=witness.witness_id, detail=detail)
        )

    if witness.schema_version != WITNESS_SCHEMA_VERSION:
        reject(
            WitnessProblem.SCHEMA_VERSION_UNKNOWN,
            f"witness schema {witness.schema_version!r} is not "
            f"{WITNESS_SCHEMA_VERSION!r}; its fields cannot be read with confidence",
        )

    if current_fingerprint is not None and witness.worktree_fingerprint != (
        current_fingerprint
    ):
        reject(
            WitnessProblem.STALE_FINGERPRINT,
            f"the witness observed worktree {witness.worktree_fingerprint[:12]} but "
            f"the candidate is {current_fingerprint[:12]}; it describes code that "
            "has since changed",
        )

    if not witness.passed and witness.criteria_proved:
        reject(
            WitnessProblem.FAILED_WITNESS_CLAIMS_PROOF,
            "a failing witness claims to prove "
            f"{', '.join(witness.criteria_proved)}",
        )

    # The central rule. A witness whose only content is its own name and an
    # exit code is exactly the `behavioral-integration` case.
    has_substance = bool(
        witness.exchanges
        or (witness.coverage and witness.coverage.executed_paths)
        or witness.process
        or witness.artifact_sha256
    )
    if not has_substance and witness.kind != WitnessKind.STATIC:
        reject(
            WitnessProblem.COMMAND_NAME_ONLY,
            f"witness {witness.command_name!r} records no routes, no coverage, no "
            "process, and no artifacts; its name is not evidence that anything "
            "happened",
        )

    if witness.coverage is not None and not witness.coverage.collection_method:
        reject(
            WitnessProblem.NO_COVERAGE_METHOD,
            "coverage was asserted without naming how it was collected, so it "
            "cannot be distinguished from a hand-written list",
        )

    if witness.process is not None:
        if not witness.process.readiness_condition.strip():
            reject(
                WitnessProblem.LAUNCH_WITHOUT_READINESS,
                "the process has no readiness condition, so the run may have "
                "raced the server it was testing",
            )
        if not witness.process.cleaned_up:
            reject(
                WitnessProblem.LAUNCH_NOT_CLEANED_UP,
                "the launched process was not cleaned up; a server left running "
                "can make a later witness pass for the wrong reason",
            )

    # A mutation nobody read back proves the endpoint accepted a request, not
    # that anything persisted. Crisis Atlas shipped exactly that shape.
    for mutation in witness.mutations:
        followed = any(
            item.route == mutation.route and not item.mutating
            for item in witness.exchanges
        )
        if not followed:
            reject(
                WitnessProblem.MUTATION_NEVER_RE_READ,
                f"{mutation.method} {mutation.route} mutated state and nothing read "
                "it back, so persistence is unproven",
            )

    if witness.criteria_proved and not has_substance:
        reject(
            WitnessProblem.CLAIMS_WITHOUT_EVIDENCE,
            "the witness claims criteria without recording anything it did",
        )
    return problems


def require_witness(
    witness: StructuredWitness, *, current_fingerprint: str | None = None
) -> StructuredWitness:
    """Fail closed. The wrapper must produce its declared evidence or nothing."""

    problems = validate_witness(witness, current_fingerprint=current_fingerprint)
    if problems:
        raise WitnessRefused(
            f"witness {witness.witness_id!r} is not usable as evidence: "
            + "; ".join(item.detail for item in problems)
        )
    return witness


def usable_witnesses(
    witnesses: list[StructuredWitness], *, current_fingerprint: str | None = None
) -> tuple[list[StructuredWitness], list[WitnessRejection]]:
    """Split a set into what may be used and why the rest may not."""

    usable: list[StructuredWitness] = []
    rejected: list[WitnessRejection] = []
    for witness in witnesses:
        problems = validate_witness(witness, current_fingerprint=current_fingerprint)
        if problems:
            rejected.extend(problems)
        else:
            usable.append(witness)
    return usable, rejected

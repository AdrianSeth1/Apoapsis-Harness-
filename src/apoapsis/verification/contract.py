"""Deterministic assessment of how much a configured verification contract
can actually prove (ADR 0069).

Live task ``TASK-33E0EB6476C4`` (2026-07-27) reached a formally correct
``COMPLETE`` on a browser application that did not run at all. Seven
owner-written checks passed. Each of them asserted that some fragment of
HTML, CSS, or JavaScript existed; none of them asserted that the fragments
referred to each other, and none of them executed the product. The harness
behaved exactly as configured, and the configuration could not tell a
working application from a pile of plausible-looking source files.

The lesson is not that the harness should judge product quality -- it
cannot, and pretending otherwise would be a worse failure than the one
being fixed. The lesson is that the *strength of the configured evidence*
is itself a fact the harness can compute deterministically, and that fact
must be attached to the outcome instead of being left for a human to infer
from an empty acceptance-coverage table.

What this module can and cannot see, stated plainly, because the
distinction is the whole point:

* It CAN see structure: whether any command is required, whether any
  command carries the owner's ``acceptance = true`` designation, whether
  every active acceptance criterion is mapped to such a command, and
  whether the configured completion policy gates on those mappings.
* It CANNOT see behavior. Nothing here inspects a command's argv to guess
  whether it "really" exercises the product. A command named
  ``browser-behavior`` that greps a file is indistinguishable from one
  that drives a browser, and any heuristic claiming otherwise would be a
  fabricated assurance. `apoapsis.verification.web_product` exists to give
  an owner a real check to configure; this module only reports whether
  something with acceptance authority was configured at all.

Nothing here blocks execution or changes an outcome. It produces a record.
The owner decides what to do about it.

ADR 0073 adds one more thing this module can see, and it is worth being
precise about what kind of thing it is. `CRITERION_ASKS_FOR_BEHAVIOR` reads
the *owner's own criterion text* for words that describe runtime behavior --
persistence, browser/API integration, interaction. That is not a heuristic
about a command's argv, which this module still refuses to attempt; it is
reading back what the owner already wrote, and asking whether a check that
never executes the product can plausibly prove it.

It is deliberately a WARNING that never changes `evidence_level`, and it
will produce false positives: a criterion may say "saved" about something a
unit test proves perfectly well. The cost of a false positive here is one
line an owner reads and dismisses. The cost of the false negative it exists
to prevent was Crisis Atlas: a criterion requiring an incident to survive a
browser reload, mapped to a static file check, reported as proven.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field

from apoapsis.config import CompletionPolicy
from apoapsis.specification.schema import StrictModel, TaskSpecification
from apoapsis.verification.runner import VerificationCommand


class ContractFindingSeverity(StrEnum):
    """How much weight a finding deserves in a human's decision.

    ``CRITICAL`` never means "the harness refuses"; it means "a reported
    success from this contract carries materially less evidence than the
    word 'complete' usually implies".
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ContractFindingCode(StrEnum):
    NO_VERIFICATION_COMMAND = "no_verification_command"
    NO_REQUIRED_COMMAND = "no_required_command"
    NO_ACCEPTANCE_COMMAND = "no_acceptance_command"
    UNMAPPED_ACCEPTANCE_CRITERION = "unmapped_acceptance_criterion"
    CRITERION_MAPPED_TO_UNKNOWN_COMMAND = "criterion_mapped_to_unknown_command"
    CRITERION_MAPPED_TO_NON_ACCEPTANCE_COMMAND = (
        "criterion_mapped_to_non_acceptance_command"
    )
    COMPLETION_POLICY_IGNORES_CRITERIA = "completion_policy_ignores_criteria"
    ONE_COMMAND_PROVES_EVERY_CRITERION = "one_command_proves_every_criterion"
    CRITERION_ASKS_FOR_BEHAVIOR = "criterion_asks_for_behavior"


class ContractEvidenceLevel(StrEnum):
    """The strongest claim this contract could support if everything passed.

    Deliberately ordered from weakest to strongest, and deliberately
    unrelated to whether any command has actually run yet -- this is a
    property of the configuration, not of a particular execution.
    """

    NONE = "none"
    DEVELOPMENT_ONLY = "development_only"
    ACCEPTANCE_DESIGNATED = "acceptance_designated"
    CRITERION_MAPPED = "criterion_mapped"


_EVIDENCE_LEVEL_ORDER: tuple[ContractEvidenceLevel, ...] = (
    ContractEvidenceLevel.NONE,
    ContractEvidenceLevel.DEVELOPMENT_ONLY,
    ContractEvidenceLevel.ACCEPTANCE_DESIGNATED,
    ContractEvidenceLevel.CRITERION_MAPPED,
)


class ContractFinding(StrictModel):
    code: ContractFindingCode
    severity: ContractFindingSeverity
    detail: str = Field(min_length=1)
    remediation: str = Field(min_length=1)
    subject: str | None = Field(
        default=None,
        description=(
            "The criterion id or command name this finding is about, when "
            "it is about one specific thing rather than the contract as a "
            "whole."
        ),
    )


class VerificationContractAssessment(StrictModel):
    """A harness-computed, model-independent statement of evidence strength."""

    schema_version: str = "1.0"
    evidence_level: ContractEvidenceLevel
    completion_policy: CompletionPolicy
    configured_command_names: list[str] = Field(default_factory=list)
    required_command_names: list[str] = Field(default_factory=list)
    acceptance_command_names: list[str] = Field(default_factory=list)
    active_criteria: int = Field(default=0, ge=0)
    criteria_mapped_to_acceptance_command: int = Field(default=0, ge=0)
    findings: list[ContractFinding] = Field(default_factory=list)
    qualification: str = Field(min_length=1)

    @property
    def critical_findings(self) -> list[ContractFinding]:
        return [
            item
            for item in self.findings
            if item.severity == ContractFindingSeverity.CRITICAL
        ]

    @property
    def proves_configured_criteria(self) -> bool:
        """True only when passing this contract would actually prove every
        active acceptance criterion. A ``COMPLETE`` produced by a contract
        for which this is False is real -- the configured checks did pass --
        but it is a weaker claim than the word suggests, and every surface
        that reports it should say so."""

        return self.evidence_level == ContractEvidenceLevel.CRITERION_MAPPED


# Words in an owner's criterion text that describe something happening at
# runtime. Kept as an explicit, readable table rather than a cleverer
# classifier precisely so an owner who disagrees with a finding can look at
# this list and see exactly why it fired (ADR 0073).
#
# Matched against whole words in lowercased criterion text. Deliberately
# conservative: "renders" and "displays" are absent because a static check
# can reasonably speak to markup, and including them would fire on almost
# every UI criterion ever written.
_BEHAVIORAL_CRITERION_WORDS: dict[str, str] = {
    "persist": "persistence",
    "persists": "persistence",
    "persisted": "persistence",
    "persistence": "persistence",
    "reload": "surviving a reload",
    "reloads": "surviving a reload",
    "restart": "surviving a restart",
    "restarts": "surviving a restart",
    "survive": "surviving a restart or reload",
    "survives": "surviving a restart or reload",
    "round-trip": "a round trip through the API",
    "round-trips": "a round trip through the API",
    "roundtrip": "a round trip through the API",
    "api": "browser/API integration",
    "endpoint": "browser/API integration",
    "endpoints": "browser/API integration",
    "backend": "browser/API integration",
    "server": "browser/API integration",
    "click": "interaction behavior",
    "clicks": "interaction behavior",
    "clicking": "interaction behavior",
    "submit": "interaction behavior",
    "submits": "interaction behavior",
    "submitting": "interaction behavior",
    "interact": "interaction behavior",
    "interacts": "interaction behavior",
    "database": "persistence",
    "stored": "persistence",
    "saves": "persistence",
    "saved": "persistence",
}

_CRITERION_WORD = re.compile(r"[A-Za-z][A-Za-z-]*")


def behavioral_concerns(text: str) -> list[str]:
    """Which runtime concerns an owner's criterion text mentions.

    Returns a sorted, de-duplicated list of concern names, empty when the
    criterion reads as something a static check could prove. Exported so a
    test can assert the table's behaviour directly rather than only through
    a full assessment.
    """

    found = {
        _BEHAVIORAL_CRITERION_WORDS[word]
        for word in (
            match.group(0).lower() for match in _CRITERION_WORD.finditer(text)
        )
        if word in _BEHAVIORAL_CRITERION_WORDS
    }
    return sorted(found)


_QUALIFICATIONS: dict[ContractEvidenceLevel, str] = {
    ContractEvidenceLevel.NONE: (
        "No configured check can prove anything about this task: a reported "
        "success would rest on no deterministic evidence at all."
    ),
    ContractEvidenceLevel.DEVELOPMENT_ONLY: (
        "Configured checks are development gates only. Passing them means "
        "the configured commands exited zero -- it does not mean any "
        "acceptance criterion was proven, because no command carries "
        "acceptance authority."
    ),
    ContractEvidenceLevel.ACCEPTANCE_DESIGNATED: (
        "Acceptance-designated commands exist but do not cover every active "
        "acceptance criterion, so a reported success proves the criteria "
        "that are mapped and leaves the rest unproven."
    ),
    ContractEvidenceLevel.CRITERION_MAPPED: (
        "Every active acceptance criterion is mapped to an owner-approved "
        "acceptance command, so passing this contract proves each criterion "
        "to the strength of the command the owner designated for it."
    ),
}


def assess_verification_contract(
    specification: TaskSpecification | None,
    commands: list[VerificationCommand],
    completion_policy: CompletionPolicy = CompletionPolicy.BASELINE,
) -> VerificationContractAssessment:
    """Grade a configured contract's evidence structure. Pure and total.

    ``specification`` may be ``None`` for a project-wide assessment (Doctor
    runs before any task exists), in which case criterion-mapping findings
    are simply not produced -- absence of a task is not evidence of a weak
    contract.
    """

    configured = [item.name for item in commands]
    required = [item.name for item in commands if item.required]
    acceptance = [item.name for item in commands if item.acceptance]
    configured_set = set(configured)
    acceptance_set = set(acceptance)

    findings: list[ContractFinding] = []
    if not commands:
        findings.append(
            ContractFinding(
                code=ContractFindingCode.NO_VERIFICATION_COMMAND,
                severity=ContractFindingSeverity.CRITICAL,
                detail="no verification command is configured",
                remediation=(
                    "add at least one [[verification.commands]] entry that "
                    "actually exercises this project"
                ),
            )
        )
    elif not required:
        findings.append(
            ContractFinding(
                code=ContractFindingCode.NO_REQUIRED_COMMAND,
                severity=ContractFindingSeverity.CRITICAL,
                detail=(
                    "no configured verification command is required, so no "
                    "command failure can gate completion"
                ),
                remediation="set required = true on at least one command",
            )
        )

    if commands and not acceptance:
        findings.append(
            ContractFinding(
                code=ContractFindingCode.NO_ACCEPTANCE_COMMAND,
                severity=ContractFindingSeverity.CRITICAL,
                detail=(
                    "no configured command is marked acceptance = true, so "
                    "no acceptance criterion can ever be proven and "
                    "acceptance coverage will always be empty"
                ),
                remediation=(
                    "decide which command's pass is real product proof, mark "
                    "it acceptance = true, and map each criterion's "
                    "verification_method to it"
                ),
            )
        )

    criteria = list(specification.active_acceptance_criteria) if specification else []
    mapped = 0
    for criterion in criteria:
        method = criterion.verification_method
        if method is None:
            findings.append(
                ContractFinding(
                    code=ContractFindingCode.UNMAPPED_ACCEPTANCE_CRITERION,
                    severity=ContractFindingSeverity.WARNING,
                    subject=criterion.id,
                    detail=(
                        f"{criterion.id} has no verification_method, so "
                        "nothing deterministic proves it"
                    ),
                    remediation=(
                        "map this criterion to an acceptance-designated "
                        "verification command"
                    ),
                )
            )
        elif method not in configured_set:
            findings.append(
                ContractFinding(
                    code=ContractFindingCode.CRITERION_MAPPED_TO_UNKNOWN_COMMAND,
                    severity=ContractFindingSeverity.WARNING,
                    subject=criterion.id,
                    detail=(
                        f"{criterion.id} names verification command "
                        f"{method!r}, which is not configured"
                    ),
                    remediation=(
                        f"configure a command named {method!r} or remap the "
                        "criterion"
                    ),
                )
            )
        elif method not in acceptance_set:
            findings.append(
                ContractFinding(
                    code=(
                        ContractFindingCode
                        .CRITERION_MAPPED_TO_NON_ACCEPTANCE_COMMAND
                    ),
                    severity=ContractFindingSeverity.WARNING,
                    subject=criterion.id,
                    detail=(
                        f"{criterion.id} names {method!r}, which is "
                        "configured but not marked acceptance = true, so its "
                        "pass cannot prove the criterion"
                    ),
                    remediation=(
                        f"mark {method!r} acceptance = true once you have "
                        "decided its pass is real proof, or remap the "
                        "criterion"
                    ),
                )
            )
        else:
            mapped += 1

        # ADR 0073. Raised whether or not the criterion is mapped: a
        # criterion about persistence proved by *any* configured command is
        # worth a second look, and an unmapped one is already covered by a
        # separate finding without saying why it is hard to map. Never
        # affects `evidence_level` -- this is a question for the owner, not
        # a grade.
        concerns = behavioral_concerns(criterion.text)
        if concerns:
            findings.append(
                ContractFinding(
                    code=ContractFindingCode.CRITERION_ASKS_FOR_BEHAVIOR,
                    severity=ContractFindingSeverity.WARNING,
                    subject=criterion.id,
                    detail=(
                        f"{criterion.id} describes {', '.join(concerns)}, "
                        "which no check that never executes the product can "
                        "prove; a static cross-reference can show the files "
                        "agree with each other and nothing more"
                    ),
                    remediation=(
                        "configure a project-specific acceptance command "
                        "that exercises this behavior end to end, and map "
                        f"{criterion.id} to it; if the criterion is in fact "
                        "provable statically, this warning can be ignored"
                    ),
                )
            )

    if criteria and mapped == len(criteria) and len(acceptance_set) == 1 and mapped > 1:
        only = sorted(acceptance_set)[0]
        findings.append(
            ContractFinding(
                code=ContractFindingCode.ONE_COMMAND_PROVES_EVERY_CRITERION,
                severity=ContractFindingSeverity.INFO,
                subject=only,
                detail=(
                    f"all {mapped} active acceptance criteria are proven by "
                    f"the single command {only!r}; the contract cannot "
                    "distinguish which criterion a failure belongs to"
                ),
                remediation=(
                    "consider one acceptance command per criterion when a "
                    "criterion needs to be attributable on its own"
                ),
            )
        )

    if criteria and completion_policy == CompletionPolicy.BASELINE:
        findings.append(
            ContractFinding(
                code=ContractFindingCode.COMPLETION_POLICY_IGNORES_CRITERIA,
                severity=ContractFindingSeverity.WARNING,
                detail=(
                    f"completion_policy is baseline, so COMPLETE is reported "
                    f"when the configured commands pass even though this "
                    f"task has {len(criteria)} active acceptance "
                    "criteria that are not required to be proven"
                ),
                remediation=(
                    'set completion_policy = "strict" in [execution] once '
                    "real acceptance commands exist (ADR 0015/0016/0017)"
                ),
            )
        )

    if not commands or not required:
        level = ContractEvidenceLevel.NONE
    elif not acceptance:
        level = ContractEvidenceLevel.DEVELOPMENT_ONLY
    elif criteria and mapped == len(criteria):
        level = ContractEvidenceLevel.CRITERION_MAPPED
    elif not criteria:
        # Acceptance-designated commands exist and there is nothing to map:
        # for a task with no active criteria this is as strong as the
        # configuration can be, and `acceptance_coverage_satisfied` already
        # treats an empty coverage list as vacuously satisfied.
        level = ContractEvidenceLevel.CRITERION_MAPPED
    else:
        level = ContractEvidenceLevel.ACCEPTANCE_DESIGNATED

    return VerificationContractAssessment(
        evidence_level=level,
        completion_policy=completion_policy,
        configured_command_names=configured,
        required_command_names=required,
        acceptance_command_names=acceptance,
        active_criteria=len(criteria),
        criteria_mapped_to_acceptance_command=mapped,
        findings=findings,
        qualification=_QUALIFICATIONS[level],
    )


def evidence_level_at_least(
    level: ContractEvidenceLevel, minimum: ContractEvidenceLevel
) -> bool:
    return _EVIDENCE_LEVEL_ORDER.index(level) >= _EVIDENCE_LEVEL_ORDER.index(minimum)


__all__ = [
    "ContractEvidenceLevel",
    "ContractFinding",
    "ContractFindingCode",
    "ContractFindingSeverity",
    "VerificationContractAssessment",
    "assess_verification_contract",
    "behavioral_concerns",
    "evidence_level_at_least",
]

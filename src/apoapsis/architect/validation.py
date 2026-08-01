from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterable, Sequence

from apoapsis.architect.schema import (
    ArchitecturePlan,
    ImplementationSlice,
    PlanValidationFinding,
    PlanValidationResult,
    PlanRecord,
    RuntimeBoundary,
    ValidationSeverity,
)
from apoapsis.config import ApoapsisConfig, ArchitectPlanCeilings
from apoapsis.specification.schema import ConstraintStatus
from apoapsis.verification.runner import VerificationCommand

if TYPE_CHECKING:
    from apoapsis.architect.store import SQLitePlanStore

# Which harness-owned verification flags contradict which declared runtime
# boundaries (ADR 0074). Keyed by flag because the harness defines these
# flags and knows exactly what they forbid -- this is not an inference
# about an arbitrary command's behaviour, which the codebase refuses to
# make, but a lookup of Apoapsis's own documented option semantics
# (ADR 0073).
_BOUNDARY_FORBIDDING_FLAGS: dict[str, frozenset[RuntimeBoundary]] = {
    "--forbid-runtime-network-apis": frozenset(
        {RuntimeBoundary.SAME_ORIGIN_HTTP, RuntimeBoundary.CROSS_ORIGIN_HTTP}
    ),
    "--forbid-external-resources": frozenset({RuntimeBoundary.CROSS_ORIGIN_HTTP}),
}


def _is_safe_relative_path(path: str) -> bool:
    """Repository-relative, non-escaping path check.

    Advisory suggested paths must never be able to point outside the
    repository: no NUL bytes, no absolute paths (POSIX or a Windows drive
    letter), and no ``..`` path segment.
    """

    if not path or "\x00" in path:
        return False
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if len(normalized) >= 2 and normalized[1] == ":":
        return False
    return ".." not in PurePosixPath(normalized).parts


def _slice_graph(slices: Sequence[ImplementationSlice]) -> dict[str, list[str]]:
    return {item.slice_id: list(item.dependencies) for item in slices}


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """DFS cycle detection over known slice IDs only; a dependency naming an
    unknown slice is reported separately as a missing dependency and is not
    treated as a graph edge here."""

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    stack_path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack_path.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in graph:
                continue
            state = color.get(neighbor, WHITE)
            if state == GRAY:
                cycle_start = stack_path.index(neighbor)
                return stack_path[cycle_start:] + [neighbor]
            if state == WHITE:
                found = visit(neighbor)
                if found:
                    return found
        stack_path.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def _longest_dependency_depth(graph: dict[str, list[str]]) -> int:
    """Longest path length (edge count) in the dependency DAG. Callers must
    confirm the graph is acyclic first; a cyclic graph would recurse
    forever without the ``visiting`` guard below, so it degrades to 0 for
    any node already on the current path rather than looping."""

    memo: dict[str, int] = {}

    def depth(node: str, visiting: frozenset[str]) -> int:
        if node in memo:
            return memo[node]
        if node in visiting:
            return 0
        next_visiting = visiting | {node}
        best = 0
        for neighbor in graph.get(node, []):
            if neighbor not in graph:
                continue
            best = max(best, 1 + depth(neighbor, next_visiting))
        memo[node] = best
        return best

    return max((depth(node, frozenset()) for node in graph), default=0)


def validate_plan(
    plan: ArchitecturePlan,
    *,
    configured_verification_commands: Iterable[str],
    ceilings: ArchitectPlanCeilings,
    configured_commands: Sequence[VerificationCommand] | None = None,
) -> list[PlanValidationFinding]:
    """Deterministic, harness-owned plan validation (ADR 0019).

    Returns findings rather than raising: an invalid plan is still stored
    and inspectable, with concrete errors a human (and, on a later
    correction pass, the planner) can act on. Only ``PlanValidationResult
    .valid`` -- computed from the presence of ``ValidationSeverity.ERROR``
    findings -- ever gates approval.

    ``configured_commands`` carries the full command objects, whose ``argv``
    the ADR 0074 cross-consistency checks need in order to see that a
    configured check forbids the very mechanism an integration contract
    declares. It is optional and defaults to ``None`` so an existing caller
    that only has command *names* keeps working, with those checks simply
    not produced -- absent information is not evidence of a contradiction.
    """

    findings: list[PlanValidationFinding] = []
    configured_names = set(configured_verification_commands)

    def error(code: str, message: str, *, slice_id: str | None = None) -> None:
        findings.append(
            PlanValidationFinding(
                severity=ValidationSeverity.ERROR,
                code=code,
                message=message,
                slice_id=slice_id,
            )
        )

    decision_ids = [item.decision_id for item in plan.decisions]
    slice_ids = [item.slice_id for item in plan.slices]
    constraint_ids = [item.id for item in plan.hard_constraints]
    criterion_ids = [item.id for item in plan.acceptance_criteria]
    component_ids = [item.component_id for item in plan.components]
    contract_ids = [item.contract_id for item in plan.integration_contracts]
    problem_ids = [item.problem_id for item in plan.anticipated_hard_problems]
    for label, ids in (
        ("decision", decision_ids),
        ("slice", slice_ids),
        ("hard constraint", constraint_ids),
        ("acceptance criterion", criterion_ids),
        ("architecture component", component_ids),
        ("integration contract", contract_ids),
        ("anticipated hard problem", problem_ids),
    ):
        seen: set[str] = set()
        for identifier in ids:
            if identifier in seen:
                error("DUPLICATE_ID", f"duplicate {label} ID: {identifier}")
            seen.add(identifier)

    known_slice_ids = set(slice_ids)
    known_constraint_ids = set(constraint_ids)
    known_criterion_ids = set(criterion_ids)
    known_component_ids = set(component_ids)
    known_contract_ids = set(contract_ids)
    known_problem_ids = set(problem_ids)
    active_constraint_ids = {
        item.id
        for item in plan.hard_constraints
        if item.status == ConstraintStatus.ACTIVE
    }

    for component in plan.components:
        for dependency in component.dependencies:
            if dependency not in known_component_ids:
                error(
                    "UNKNOWN_COMPONENT_REFERENCE",
                    f"component {component.component_id} depends on unknown "
                    f"component {dependency}",
                )

    for contract in plan.integration_contracts:
        if contract.producer_component_id not in known_component_ids:
            error(
                "UNKNOWN_COMPONENT_REFERENCE",
                f"integration contract {contract.contract_id} names unknown "
                f"producer component {contract.producer_component_id}",
            )
        for consumer_id in contract.consumer_component_ids:
            if consumer_id not in known_component_ids:
                error(
                    "UNKNOWN_COMPONENT_REFERENCE",
                    f"integration contract {contract.contract_id} names "
                    f"unknown consumer component {consumer_id}",
                )

    for problem in plan.anticipated_hard_problems:
        for component_id in problem.affected_component_ids:
            if component_id not in known_component_ids:
                error(
                    "UNKNOWN_COMPONENT_REFERENCE",
                    f"anticipated hard problem {problem.problem_id} names "
                    f"unknown affected component {component_id}",
                )

    # verification_strategy is always a real (possibly empty) instance --
    # PlanDeliveryContract/RuntimeDesign/VerificationStrategy all default
    # via default_factory, never None -- so these loops simply no-op on a
    # plan that left the section blank.
    for obligation in plan.verification_strategy.acceptance_proof_obligations:
        if obligation.criterion_id not in known_criterion_ids:
            error(
                "UNKNOWN_CRITERION_REFERENCE",
                "verification strategy references unknown acceptance "
                f"criterion {obligation.criterion_id}",
            )
        for command_name in obligation.verification_commands:
            if command_name not in configured_names:
                error(
                    "UNKNOWN_VERIFICATION_COMMAND",
                    "verification strategy names verification command "
                    f"{command_name!r}, which is not configured",
                )
    for scenario in plan.verification_strategy.end_to_end_scenarios:
        for command_name in scenario.verification_commands:
            if command_name not in configured_names:
                error(
                    "UNKNOWN_VERIFICATION_COMMAND",
                    f"end-to-end scenario {scenario.scenario_id} names "
                    f"verification command {command_name!r}, which is "
                    "not configured",
                )
    for command_name in plan.verification_strategy.whole_project_verification_commands:
        if command_name not in configured_names:
            error(
                "UNKNOWN_VERIFICATION_COMMAND",
                "verification strategy names whole-project verification "
                f"command {command_name!r}, which is not configured",
            )

    if len(plan.slices) > ceilings.max_slices:
        error(
            "TOO_MANY_SLICES",
            f"plan has {len(plan.slices)} slices, exceeding the configured "
            f"ceiling of {ceilings.max_slices}",
        )

    represented_constraint_ids: set[str] = set()
    for item in plan.slices:
        for dependency in item.dependencies:
            if dependency not in known_slice_ids:
                error(
                    "MISSING_DEPENDENCY",
                    f"slice {item.slice_id} depends on unknown slice "
                    f"{dependency}",
                    slice_id=item.slice_id,
                )

        for constraint_id in item.inherited_constraint_ids:
            if constraint_id not in known_constraint_ids:
                error(
                    "UNKNOWN_CONSTRAINT_REFERENCE",
                    f"slice {item.slice_id} references unknown hard "
                    f"constraint {constraint_id}",
                    slice_id=item.slice_id,
                )
            else:
                represented_constraint_ids.add(constraint_id)

        for criterion_id in item.acceptance_criterion_ids:
            if criterion_id not in known_criterion_ids:
                error(
                    "UNKNOWN_CRITERION_REFERENCE",
                    f"slice {item.slice_id} references unknown acceptance "
                    f"criterion {criterion_id}",
                    slice_id=item.slice_id,
                )

        for component_id in item.architecture_component_ids:
            if component_id not in known_component_ids:
                error(
                    "UNKNOWN_COMPONENT_REFERENCE",
                    f"slice {item.slice_id} references unknown architecture "
                    f"component {component_id}",
                    slice_id=item.slice_id,
                )

        for contract_id in item.integration_contract_ids:
            if contract_id not in known_contract_ids:
                error(
                    "UNKNOWN_CONTRACT_REFERENCE",
                    f"slice {item.slice_id} references unknown integration "
                    f"contract {contract_id}",
                    slice_id=item.slice_id,
                )

        for problem_id in item.hard_problem_ids:
            if problem_id not in known_problem_ids:
                error(
                    "UNKNOWN_HARD_PROBLEM_REFERENCE",
                    f"slice {item.slice_id} references unknown anticipated "
                    f"hard problem {problem_id}",
                    slice_id=item.slice_id,
                )

        for command_name in item.verification_commands:
            if command_name not in configured_names:
                error(
                    "UNKNOWN_VERIFICATION_COMMAND",
                    f"slice {item.slice_id} names verification command "
                    f"{command_name!r}, which is not configured",
                    slice_id=item.slice_id,
                )

        if not item.verification_commands:
            error(
                "MISSING_VERIFICATION_INTENT",
                f"slice {item.slice_id} names no verification command",
                slice_id=item.slice_id,
            )

        # A slice that inherits no acceptance criterion has no
        # human-approved definition of done: the coding agent is told what
        # to build and which command will run, but nothing states what that
        # command must prove. Naming a verification command is not a
        # substitute -- an empty suite passes one. Symmetric with
        # MISSING_VERIFICATION_INTENT above, and an ERROR for the same
        # reason: an unfalsifiable slice must not reach approval.
        if not item.acceptance_criterion_ids:
            error(
                "MISSING_ACCEPTANCE_CRITERIA",
                f"slice {item.slice_id} inherits no acceptance criterion, so "
                "its completion cannot be judged against the approved plan",
                slice_id=item.slice_id,
            )

        # Test obligations are the slice's own statement of what it must
        # leave behind for its verification command to be meaningful.
        # Without them a slice can satisfy a green run by shipping no tests
        # at all.
        if not item.test_obligations:
            error(
                "MISSING_TEST_OBLIGATIONS",
                f"slice {item.slice_id} declares no test obligation, so its "
                "verification command could pass without exercising the slice",
                slice_id=item.slice_id,
            )

        for path in item.suggested_paths:
            if not _is_safe_relative_path(path):
                error(
                    "UNSAFE_SUGGESTED_PATH",
                    f"slice {item.slice_id} suggests an unsafe path: {path!r}",
                    slice_id=item.slice_id,
                )

        if len(item.suggested_paths) > ceilings.max_suggested_paths_per_slice:
            error(
                "TOO_MANY_SUGGESTED_PATHS",
                f"slice {item.slice_id} suggests {len(item.suggested_paths)} "
                "paths, exceeding the configured ceiling of "
                f"{ceilings.max_suggested_paths_per_slice}",
                slice_id=item.slice_id,
            )

        criteria_count = len(item.inherited_constraint_ids) + len(
            item.acceptance_criterion_ids
        )
        if criteria_count > ceilings.max_criteria_per_slice:
            error(
                "TOO_MANY_CRITERIA",
                f"slice {item.slice_id} references {criteria_count} "
                "constraints/criteria, exceeding the configured ceiling of "
                f"{ceilings.max_criteria_per_slice}",
                slice_id=item.slice_id,
            )

        if len(item.work_brief) > ceilings.max_work_brief_chars:
            error(
                "WORK_BRIEF_TOO_LONG",
                f"slice {item.slice_id} work_brief is "
                f"{len(item.work_brief)} characters, exceeding the "
                f"configured ceiling of {ceilings.max_work_brief_chars}",
                slice_id=item.slice_id,
            )

    for constraint_id in sorted(active_constraint_ids - represented_constraint_ids):
        error(
            "UNREPRESENTED_HARD_CONSTRAINT",
            f"active hard constraint {constraint_id} is not inherited by "
            "any slice",
        )

    graph = _slice_graph(plan.slices)
    cycle = _find_cycle(graph)
    if cycle:
        error("DEPENDENCY_CYCLE", "slice dependency cycle: " + " -> ".join(cycle))
    else:
        depth = _longest_dependency_depth(graph)
        if depth > ceilings.max_dependency_depth:
            error(
                "DEPENDENCY_DEPTH_EXCEEDED",
                f"slice dependency depth is {depth}, exceeding the "
                f"configured ceiling of {ceilings.max_dependency_depth}",
            )

    # -- ADR 0074 cross-consistency ------------------------------------
    #
    # Everything below reads structured fields only. No check here infers
    # an architectural requirement from `interface`, `data_flow`,
    # `objective`, or any other prose: the whole reason `runtime_boundary`
    # exists is so a contradiction can be found without guessing at
    # sentences. ADR 0073's keyword-based criterion warning stays advisory
    # and is deliberately absent from this gate.

    for scenario in plan.verification_strategy.end_to_end_scenarios:
        for command_name in scenario.verification_commands:
            if command_name not in configured_names:
                error(
                    "UNKNOWN_VERIFICATION_COMMAND",
                    f"end-to-end scenario {scenario.scenario_id} names "
                    f"verification command {command_name!r}, which is "
                    "not configured",
                )
    for command_name in plan.verification_strategy.whole_project_verification_commands:
        if command_name not in configured_names:
            error(
                "UNKNOWN_VERIFICATION_COMMAND",
                "verification strategy names whole-project verification "
                f"command {command_name!r}, which is not configured",
            )

    if len(plan.slices) > ceilings.max_slices:
        error(
            "TOO_MANY_SLICES",
            f"plan has {len(plan.slices)} slices, exceeding the configured "
            f"ceiling of {ceilings.max_slices}",
        )

    represented_constraint_ids: set[str] = set()
    for item in plan.slices:
        for dependency in item.dependencies:
            if dependency not in known_slice_ids:
                error(
                    "MISSING_DEPENDENCY",
                    f"slice {item.slice_id} depends on unknown slice "
                    f"{dependency}",
                    slice_id=item.slice_id,
                )

        for constraint_id in item.inherited_constraint_ids:
            if constraint_id not in known_constraint_ids:
                error(
                    "UNKNOWN_CONSTRAINT_REFERENCE",
                    f"slice {item.slice_id} references unknown hard "
                    f"constraint {constraint_id}",
                    slice_id=item.slice_id,
                )
            else:
                represented_constraint_ids.add(constraint_id)

        for criterion_id in item.acceptance_criterion_ids:
            if criterion_id not in known_criterion_ids:
                error(
                    "UNKNOWN_CRITERION_REFERENCE",
                    f"slice {item.slice_id} references unknown acceptance "
                    f"criterion {criterion_id}",
                    slice_id=item.slice_id,
                )

        for component_id in item.architecture_component_ids:
            if component_id not in known_component_ids:
                error(
                    "UNKNOWN_COMPONENT_REFERENCE",
                    f"slice {item.slice_id} references unknown architecture "
                    f"component {component_id}",
                    slice_id=item.slice_id,
                )

        for contract_id in item.integration_contract_ids:
            if contract_id not in known_contract_ids:
                error(
                    "UNKNOWN_CONTRACT_REFERENCE",
                    f"slice {item.slice_id} references unknown integration "
                    f"contract {contract_id}",
                    slice_id=item.slice_id,
                )

        for problem_id in item.hard_problem_ids:
            if problem_id not in known_problem_ids:
                error(
                    "UNKNOWN_HARD_PROBLEM_REFERENCE",
                    f"slice {item.slice_id} references unknown anticipated "
                    f"hard problem {problem_id}",
                    slice_id=item.slice_id,
                )

        for command_name in item.verification_commands:
            if command_name not in configured_names:
                error(
                    "UNKNOWN_VERIFICATION_COMMAND",
                    f"slice {item.slice_id} names verification command "
                    f"{command_name!r}, which is not configured",
                    slice_id=item.slice_id,
                )

        if not item.verification_commands:
            error(
                "MISSING_VERIFICATION_INTENT",
                f"slice {item.slice_id} names no verification command",
                slice_id=item.slice_id,
            )

        # A slice that inherits no acceptance criterion has no
        # human-approved definition of done: the coding agent is told what
        # to build and which command will run, but nothing states what that
        # command must prove. Naming a verification command is not a
        # substitute -- an empty suite passes one. Symmetric with
        # MISSING_VERIFICATION_INTENT above, and an ERROR for the same
        # reason: an unfalsifiable slice must not reach approval.
        if not item.acceptance_criterion_ids:
            error(
                "MISSING_ACCEPTANCE_CRITERIA",
                f"slice {item.slice_id} inherits no acceptance criterion, so "
                "its completion cannot be judged against the approved plan",
                slice_id=item.slice_id,
            )

        # Test obligations are the slice's own statement of what it must
        # leave behind for its verification command to be meaningful.
        # Without them a slice can satisfy a green run by shipping no tests
        # at all.
        if not item.test_obligations:
            error(
                "MISSING_TEST_OBLIGATIONS",
                f"slice {item.slice_id} declares no test obligation, so its "
                "verification command could pass without exercising the slice",
                slice_id=item.slice_id,
            )

        for path in item.suggested_paths:
            if not _is_safe_relative_path(path):
                error(
                    "UNSAFE_SUGGESTED_PATH",
                    f"slice {item.slice_id} suggests an unsafe path: {path!r}",
                    slice_id=item.slice_id,
                )

        if len(item.suggested_paths) > ceilings.max_suggested_paths_per_slice:
            error(
                "TOO_MANY_SUGGESTED_PATHS",
                f"slice {item.slice_id} suggests {len(item.suggested_paths)} "
                "paths, exceeding the configured ceiling of "
                f"{ceilings.max_suggested_paths_per_slice}",
                slice_id=item.slice_id,
            )

        criteria_count = len(item.inherited_constraint_ids) + len(
            item.acceptance_criterion_ids
        )
        if criteria_count > ceilings.max_criteria_per_slice:
            error(
                "TOO_MANY_CRITERIA",
                f"slice {item.slice_id} references {criteria_count} "
                "constraints/criteria, exceeding the configured ceiling of "
                f"{ceilings.max_criteria_per_slice}",
                slice_id=item.slice_id,
            )

        if len(item.work_brief) > ceilings.max_work_brief_chars:
            error(
                "WORK_BRIEF_TOO_LONG",
                f"slice {item.slice_id} work_brief is "
                f"{len(item.work_brief)} characters, exceeding the "
                f"configured ceiling of {ceilings.max_work_brief_chars}",
                slice_id=item.slice_id,
            )

    for constraint_id in sorted(active_constraint_ids - represented_constraint_ids):
        error(
            "UNREPRESENTED_HARD_CONSTRAINT",
            f"active hard constraint {constraint_id} is not inherited by "
            "any slice",
        )

    graph = _slice_graph(plan.slices)
    cycle = _find_cycle(graph)
    if cycle:
        error("DEPENDENCY_CYCLE", "slice dependency cycle: " + " -> ".join(cycle))
    else:
        depth = _longest_dependency_depth(graph)
        if depth > ceilings.max_dependency_depth:
            error(
                "DEPENDENCY_DEPTH_EXCEEDED",
                f"slice dependency depth is {depth}, exceeding the "
                f"configured ceiling of {ceilings.max_dependency_depth}",
            )

    # -- ADR 0074 cross-consistency ------------------------------------
    #
    # Everything below reads structured fields only. No check here infers
    # an architectural requirement from `interface`, `data_flow`,
    # `objective`, or any other prose: the whole reason `runtime_boundary`
    # exists is so a contradiction can be found without guessing at
    # sentences. ADR 0073's keyword-based criterion warning stays advisory
    # and is deliberately absent from this gate.

    whole_project_commands = list(
        plan.verification_strategy.whole_project_verification_commands
    )
    whole_project_command_set = set(whole_project_commands)
    if not whole_project_commands:
        # Symmetric with MISSING_VERIFICATION_INTENT for a slice, and an
        # ERROR for the same reason: a plan with no whole-project command
        # can never produce evidence about its own integrated result, and
        # per-slice history is not a substitute. Delivery refuses such a
        # plan (ADR 0074), so approving one would only defer the refusal to
        # the point where all the work is already done.
        error(
            "MISSING_WHOLE_PROJECT_VERIFICATION",
            "the plan names no whole_project_verification_commands, so "
            "nothing would ever be executed against the integrated project "
            "and delivery could not be permitted",
        )

    referenced_contract_ids = {
        contract_id
        for item in plan.slices
        for contract_id in item.integration_contract_ids
    }
    for contract in plan.integration_contracts:
        if contract.contract_id not in referenced_contract_ids:
            error(
                "UNASSIGNED_INTEGRATION_CONTRACT",
                f"integration contract {contract.contract_id} is referenced "
                "by no slice, so no slice is responsible for building or "
                "honouring it",
            )

    assigned_paths = {path for item in plan.slices for path in item.suggested_paths}
    delivery = plan.delivery_contract
    for artifact in delivery.required_artifacts:
        if artifact not in assigned_paths:
            error(
                "UNASSIGNED_DELIVERY_ARTIFACT",
                f"delivery_contract requires artifact {artifact!r}, which is "
                "not named in any slice's suggested_paths, so no slice is "
                "responsible for producing it",
            )

    # -- ADR 0076 operability contract ---------------------------------
    #
    # Structured fields only, same discipline as ADR 0074. Nothing here
    # reads `launch_or_usage_instructions` or any other prose field: those
    # remain descriptive, and Apoapsis never executes them.
    if not delivery.primary_documentation_path:
        error(
            "MISSING_PRIMARY_DOCUMENTATION",
            "delivery_contract names no primary_documentation_path, so the "
            "delivered project has no identified place an operator is meant "
            "to read first",
        )
    elif not _is_safe_relative_path(delivery.primary_documentation_path):
        error(
            "UNSAFE_PRIMARY_DOCUMENTATION_PATH",
            "delivery_contract primary_documentation_path is not a safe "
            f"repository-relative path: {delivery.primary_documentation_path!r}",
        )
    elif delivery.primary_documentation_path not in assigned_paths:
        error(
            "UNASSIGNED_DELIVERY_ARTIFACT",
            "delivery_contract names primary_documentation_path "
            f"{delivery.primary_documentation_path!r}, which is not in any "
            "slice's suggested_paths, so no slice is responsible for writing "
            "or updating it",
        )

    launch_command = delivery.launch_verification_command
    launch_excuse = delivery.launch_not_runnable_reason
    if launch_command and launch_excuse:
        error(
            "AMBIGUOUS_LAUNCH_CONTRACT",
            "delivery_contract sets both launch_verification_command "
            f"({launch_command!r}) and launch_not_runnable_reason; a plan "
            "either has a canonical launch path that can be exercised or an "
            "explicit reason it cannot, never both",
        )
    elif not launch_command and not launch_excuse:
        # Symmetric with MISSING_WHOLE_PROJECT_VERIFICATION: a deliverable
        # application whose launch is neither tested nor explicitly excused
        # is a claim nobody has to stand behind. The escape hatch is real
        # and deliberately requires the owner to write down why.
        error(
            "MISSING_LAUNCH_CONTRACT",
            "delivery_contract names neither a launch_verification_command "
            "nor a launch_not_runnable_reason, so nothing establishes that "
            "the delivered project can be started; set the command that "
            "launches or smoke-tests it, or state explicitly why no such "
            "command can exist",
        )
    elif launch_command:
        if launch_command not in configured_names:
            error(
                "UNKNOWN_VERIFICATION_COMMAND",
                "delivery_contract names launch_verification_command "
                f"{launch_command!r}, which is not configured",
            )
        elif launch_command not in whole_project_command_set:
            # A launch check that only runs inside one slice's isolated
            # worktree proves nothing about the delivered product. It has to
            # execute at the integrated commit.
            error(
                "LAUNCH_COMMAND_NOT_WHOLE_PROJECT",
                f"delivery_contract names {launch_command!r} as the launch "
                "command, but the plan does not list it in "
                "whole_project_verification_commands, so it would never run "
                "against the integrated project",
            )

    for scenario in plan.verification_strategy.end_to_end_scenarios:
        if not scenario.verification_commands:
            error(
                "UNVERIFIED_END_TO_END_SCENARIO",
                f"end-to-end scenario {scenario.scenario_id} names no "
                "verification command, so nothing proves it",
            )
            continue
        unmapped = [
            name
            for name in scenario.verification_commands
            if name not in whole_project_command_set
        ]
        if unmapped:
            # An end-to-end scenario spans more than one slice by
            # definition, so a command that only ever runs inside one
            # slice's isolated worktree cannot prove it.
            error(
                "UNVERIFIED_END_TO_END_SCENARIO",
                f"end-to-end scenario {scenario.scenario_id} is proven by "
                f"{', '.join(unmapped)}, which the plan does not list in "
                "whole_project_verification_commands; a scenario spanning "
                "slices cannot be proven by a command that only runs inside "
                "one slice",
            )

    if configured_commands is not None:
        argv_by_name = {item.name: list(item.argv) for item in configured_commands}
        acceptance_names = {
            item.name for item in configured_commands if item.acceptance
        }
        # ADR 0076. A contract that crosses a process or origin boundary at
        # runtime cannot be proven by a check that never runs the product.
        # The harness cannot detect seed data, demo-only paths, or an
        # "offline mode" fallback generically -- doing so would need exactly
        # the prose/keyword inference that is barred from gates. What it can
        # do is refuse to let such a contract exist with nothing but static
        # evidence behind it, which forces the owner to configure a command
        # that would notice.
        networked = {
            RuntimeBoundary.SAME_ORIGIN_HTTP,
            RuntimeBoundary.CROSS_ORIGIN_HTTP,
        }
        proven_end_to_end = any(
            any(
                name in acceptance_names and name in whole_project_command_set
                for name in scenario.verification_commands
            )
            for scenario in plan.verification_strategy.end_to_end_scenarios
        )
        for item in plan.integration_contracts:
            if item.runtime_boundary in networked and not proven_end_to_end:
                error(
                    "INTEGRATION_WITHOUT_END_TO_END_PROOF",
                    f"integration contract {item.contract_id} declares a "
                    f"{item.runtime_boundary.value} runtime boundary, but no "
                    "end_to_end_scenario is proven by a command that is both "
                    "acceptance-designated and run against the integrated "
                    "project; a static check cannot tell a working "
                    "integration from a plausible-looking one that never "
                    "calls the backend",
                )
        slices_by_contract: dict[str, list[ImplementationSlice]] = {}
        for item in plan.slices:
            for contract_id in item.integration_contract_ids:
                slices_by_contract.setdefault(contract_id, []).append(item)
        for contract in plan.integration_contracts:
            if contract.runtime_boundary == RuntimeBoundary.UNSPECIFIED:
                continue
            governing: set[str] = set(whole_project_commands)
            for item in slices_by_contract.get(contract.contract_id, []):
                governing.update(item.verification_commands)
            for command_name in sorted(governing):
                argv = argv_by_name.get(command_name)
                if argv is None:
                    continue
                for flag, forbidden in _BOUNDARY_FORBIDDING_FLAGS.items():
                    if flag in argv and contract.runtime_boundary in forbidden:
                        error(
                            "INTEGRATION_FORBIDDEN_BY_VERIFICATION",
                            f"integration contract {contract.contract_id} "
                            f"declares a {contract.runtime_boundary.value} "
                            f"runtime boundary, but verification command "
                            f"{command_name!r} passes {flag}, which forbids "
                            "exactly that mechanism; the plan cannot be "
                            "satisfied and passed at the same time -- change "
                            "the contract, or the command's flags",
                        )

    return findings


def validate_and_record_plan(
    project_root: str | Path,
    plan_store: "SQLitePlanStore",
    config: ApoapsisConfig,
    plan_id: str,
    *,
    expected_version: int,
) -> tuple[PlanRecord, PlanValidationResult]:
    """Run and persist the canonical deterministic plan validation.

    Shared by frontier import, the UI, and the CLI so those entry points
    cannot drift on findings, version transitions, or audit artifacts. This
    invokes no model and no project command, and never approves a plan.
    """

    from apoapsis.architect.audit import PlanAuditStore
    record = plan_store.get_plan(plan_id)
    configured_names = {command.name for command in config.verification.commands}
    findings = validate_plan(
        record.plan,
        configured_verification_commands=configured_names,
        ceilings=config.architect.ceilings,
        configured_commands=config.verification.commands,
    )
    result = PlanValidationResult(
        plan_id=plan_id,
        plan_version=record.version,
        valid=not any(
            finding.severity == ValidationSeverity.ERROR for finding in findings
        ),
        findings=findings,
    )
    updated = plan_store.record_validation(
        plan_id, result, expected_version=expected_version
    )
    PlanAuditStore(project_root, plan_id).write_json(
        f"validation-v{record.version}.json",
        result,
        kind="plan_validation_result",
    )
    return updated, result

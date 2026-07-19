from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable, Sequence

from apoapsis.architect.schema import (
    ArchitecturePlan,
    ImplementationSlice,
    PlanValidationFinding,
    ValidationSeverity,
)
from apoapsis.config import ArchitectPlanCeilings
from apoapsis.specification.schema import ConstraintStatus


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
) -> list[PlanValidationFinding]:
    """Deterministic, harness-owned plan validation (ADR 0019).

    Returns findings rather than raising: an invalid plan is still stored
    and inspectable, with concrete errors a human (and, on a later
    correction pass, the planner) can act on. Only ``PlanValidationResult
    .valid`` -- computed from the presence of ``ValidationSeverity.ERROR``
    findings -- ever gates approval.
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
    for label, ids in (
        ("decision", decision_ids),
        ("slice", slice_ids),
        ("hard constraint", constraint_ids),
        ("acceptance criterion", criterion_ids),
    ):
        seen: set[str] = set()
        for identifier in ids:
            if identifier in seen:
                error("DUPLICATE_ID", f"duplicate {label} ID: {identifier}")
            seen.add(identifier)

    known_slice_ids = set(slice_ids)
    known_constraint_ids = set(constraint_ids)
    known_criterion_ids = set(criterion_ids)
    active_constraint_ids = {
        item.id
        for item in plan.hard_constraints
        if item.status == ConstraintStatus.ACTIVE
    }

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

    return findings

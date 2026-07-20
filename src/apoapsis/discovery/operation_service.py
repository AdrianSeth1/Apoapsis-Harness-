from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig
from apoapsis.discovery.api import run_frontier_planning_api_call
from apoapsis.discovery.errors import DiscoveryError, StaleSessionError
from apoapsis.discovery.frontier_package import load_package
from apoapsis.discovery.operation_schema import (
    DiscoveryOperationAction,
    DiscoveryOperationRecord,
    research_mode_for_action,
)
from apoapsis.discovery.operation_store import DiscoveryOperationStore
from apoapsis.discovery.schema import DiscoverySessionRecord
from apoapsis.discovery.schema import DiscoveryStatus
from apoapsis.discovery.research import run_discovery_research_step
from apoapsis.discovery.service import (
    propose_idea_brief_step,
    propose_local_clarification_questions,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.operations.lease import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE_DURATION,
    LeaseHeartbeat,
    new_owner_id,
)


def prepare_discovery_operation(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    operation_store: DiscoveryOperationStore,
    config: ApoapsisConfig,
    *,
    session_id: str,
    action: DiscoveryOperationAction,
    operation_id: str,
    expected_version: int,
    authorized_max_spend_usd: float | None = None,
) -> DiscoverySessionRecord:
    """Every fast, synchronous, read-only check plus operation-record
    creation for a discovery model-call operation -- never a model call,
    mirrors ``review.execution.prepare_review_operation`` exactly. Safe to
    call directly from an HTTP request handler.
    """

    session = discovery_store.get_session(session_id)
    if session.version != expected_version:
        raise StaleSessionError(
            f"expected session version {expected_version}, found {session.version}"
        )
    package_id: str | None = None
    research_mode = research_mode_for_action(action)
    if research_mode is not None:
        if session.status != DiscoveryStatus.BRIEF_APPROVED:
            raise DiscoveryError(
                "planning research requires an approved idea brief before "
                f"it can run (found {session.status.value})"
            )
        if config.models.local_research is None:
            raise DiscoveryError(
                "planning research requires [models.local_research] configuration"
            )
    if action == DiscoveryOperationAction.FRONTIER_API_CALL:
        if authorized_max_spend_usd is None:
            raise DiscoveryError(
                "frontier_api_call requires an explicit authorized_max_spend_usd"
            )
        if session.frontier_package_id is None:
            raise DiscoveryError(
                f"session {session_id} has no outstanding frontier package"
            )
        package_id = session.frontier_package_id
    operation_store.create(
        operation_id,
        session_id,
        action,
        expected_session_version=expected_version,
        authorized_max_spend_usd=authorized_max_spend_usd,
        package_id=package_id,
    )
    return session


def run_discovery_operation(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    plan_store: SQLitePlanStore,
    config: ApoapsisConfig,
    operation_store: DiscoveryOperationStore,
    *,
    operation_id: str,
    local_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
    research_engine=None,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
) -> DiscoveryOperationRecord:
    """The actual work -- a local-model call or a frontier API call -- for
    an operation ``prepare_discovery_operation`` already validated and
    recorded. Mirrors ``review.execution.run_review_operation`` exactly:
    marks the operation ``RUNNING`` before any provider construction,
    rechecks the session's current version immediately before doing
    anything, and claims a fresh, renewed lease so a long-but-healthy call
    is never misclassified as crashed.
    """

    root_path = Path(root).resolve()
    record = operation_store.get(operation_id)
    owner_id = new_owner_id()
    operation_store.mark_running(
        operation_id, owner_id=owner_id, lease_duration=lease_duration
    )
    heartbeat = LeaseHeartbeat(
        lambda: operation_store.renew_lease(
            operation_id, owner_id=owner_id, lease_duration=lease_duration
        ),
        interval=heartbeat_interval,
    )
    heartbeat.start()
    try:
        session = discovery_store.get_session(record.session_id)
        if session.version != record.expected_session_version:
            raise StaleSessionError(
                f"session {record.session_id} changed since this operation "
                "was recorded"
            )
        if record.action == DiscoveryOperationAction.LOCAL_QUESTIONS:
            updated = propose_local_clarification_questions(
                root_path,
                discovery_store,
                config,
                record.session_id,
                expected_version=record.expected_session_version,
                local_provider=local_provider,
            )
            summary = f"proposed {len(updated.local_questions)} clarification question(s)"
        elif record.action == DiscoveryOperationAction.IDEA_BRIEF:
            updated = propose_idea_brief_step(
                root_path,
                discovery_store,
                config,
                record.session_id,
                expected_version=record.expected_session_version,
                local_provider=local_provider,
            )
            summary = "proposed an idea brief"
        elif (research_mode := research_mode_for_action(record.action)) is not None:
            updated = run_discovery_research_step(
                root_path,
                discovery_store,
                config,
                record.session_id,
                expected_version=record.expected_session_version,
                requested_mode=research_mode,
                research_engine=research_engine,
            )
            summary = (
                f"planning research completed ({research_mode.value}; "
                f"{len(updated.research_evidence_ids)} evidence item(s))"
            )
        elif record.action == DiscoveryOperationAction.FRONTIER_API_CALL:
            assert record.package_id is not None
            assert record.authorized_max_spend_usd is not None
            package = load_package(root_path, record.package_id)
            _updated, cost_usd = run_frontier_planning_api_call(
                root_path,
                discovery_store,
                plan_store,
                config,
                session_id=record.session_id,
                package=package,
                authorized_max_spend_usd=record.authorized_max_spend_usd,
                frontier_coder_provider=frontier_coder_provider,
            )
            summary = f"frontier API call completed (measured cost ${cost_usd:.4f})"
        else:
            raise AssertionError(f"unhandled discovery operation action: {record.action}")
    except Exception as exc:
        operation_store.mark_failed(
            operation_id, owner_id=owner_id, error=f"{type(exc).__name__}: {exc}"
        )
        raise
    finally:
        heartbeat.stop()
    return operation_store.mark_succeeded(
        operation_id, owner_id=owner_id, result_summary=summary
    )


def execute_discovery_operation(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    plan_store: SQLitePlanStore,
    config: ApoapsisConfig,
    operation_store: DiscoveryOperationStore,
    *,
    session_id: str,
    action: DiscoveryOperationAction,
    operation_id: str,
    expected_version: int,
    authorized_max_spend_usd: float | None = None,
    local_provider: InstrumentedModelProvider | None = None,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
    research_engine=None,
) -> DiscoveryOperationRecord:
    """Convenience wrapper for synchronous callers (the CLI): prepare and
    run in one call. The UI calls ``prepare_discovery_operation`` from its
    HTTP handler and ``run_discovery_operation`` from a background worker
    instead, so a model call never blocks a request thread."""

    prepare_discovery_operation(
        root,
        discovery_store,
        operation_store,
        config,
        session_id=session_id,
        action=action,
        operation_id=operation_id,
        expected_version=expected_version,
        authorized_max_spend_usd=authorized_max_spend_usd,
    )
    return run_discovery_operation(
        root,
        discovery_store,
        plan_store,
        config,
        operation_store,
        operation_id=operation_id,
        local_provider=local_provider,
        frontier_coder_provider=frontier_coder_provider,
        research_engine=research_engine,
    )


__all__ = [
    "execute_discovery_operation",
    "prepare_discovery_operation",
    "run_discovery_operation",
]

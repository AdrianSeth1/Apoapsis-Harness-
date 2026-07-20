from __future__ import annotations

import json
import uuid
from pathlib import Path

from pydantic import ValidationError

from apoapsis.architect.store import SQLitePlanStore
from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.discovery.audit import DiscoveryAuditStore
from apoapsis.discovery.errors import (
    DiscoveryError,
    MalformedResponseError,
    ResponseHashMismatchError,
)
from apoapsis.discovery.manual import build_frontier_planning_markdown
from apoapsis.discovery.schema import (
    DiscoverySessionRecord,
    FrontierPlanningRequestPackage,
    FrontierPlanningResponseEnvelope,
)
from apoapsis.discovery.store import SQLiteDiscoveryStore
from apoapsis.discovery.response import apply_frontier_planning_response
from apoapsis.evaluation.spend_ceiling import (
    HostedSpendCeilingExceededError,
    SpendCeilingModelProvider,
    SpendLedger,
    estimate_worst_case_call_cost_usd,
)
from apoapsis.models.base import ModelOperation
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.provider import ModelRole, ProviderInvocation
from apoapsis.models.telemetry import InstrumentedModelProvider, InstrumentedProviderError
from apoapsis.specification.schema import StrictModel


class FrontierPlanningApiNotConfiguredError(DiscoveryError):
    """Raised when the API transport is requested but no
    ``[models.frontier_coder]`` provider is configured."""


class FrontierPlanningApiPreview(StrictModel):
    """What an API planning call would authorize -- shown before a
    separate, explicit spend-ceiling authorization (mirrors ADR 0030's
    "shown before separate authorization" discipline exactly)."""

    provider: str
    model: str
    max_calls_this_round: int = 1
    worst_case_call_cost_usd: float


def _build_provider(provider_config: FrontierProviderConfig) -> InstrumentedModelProvider:
    if provider_config.provider == "ollama":
        adapter = OllamaProvider(provider_config)
    elif provider_config.provider == "openai_compatible":
        adapter = OpenAICompatibleFrontierProvider(provider_config)
    else:
        raise DiscoveryError(f"unsupported provider: {provider_config.provider}")
    return InstrumentedModelProvider(adapter, provider_config.pricing)


def preview_frontier_planning_api_call(
    config: ApoapsisConfig, package: FrontierPlanningRequestPackage
) -> FrontierPlanningApiPreview:
    """Deterministically computes what one API planning call would
    authorize -- provider/model identity and a pessimistic worst-case cost
    upper bound -- with zero calls made. Callers must show this to the
    user and obtain a separate, explicit authorization (an
    ``--authorize-planning-spend-usd`` ceiling) before
    ``run_frontier_planning_api_call`` is ever invoked."""

    provider_config = config.models.frontier_coder
    if provider_config is None:
        raise FrontierPlanningApiNotConfiguredError(
            "the API frontier planning transport requires [models.frontier_coder] "
            "to be configured; use the manual subscription transport instead"
        )
    prompt = build_frontier_planning_markdown(package)
    worst_case = estimate_worst_case_call_cost_usd(
        prompt_chars=len(prompt),
        max_output_tokens=provider_config.max_output_tokens,
        pricing=provider_config.pricing,
    )
    return FrontierPlanningApiPreview(
        provider=provider_config.provider,
        model=provider_config.model,
        worst_case_call_cost_usd=worst_case,
    )


def run_frontier_planning_api_call(
    root: str | Path,
    discovery_store: SQLiteDiscoveryStore,
    plan_store: SQLitePlanStore,
    config: ApoapsisConfig,
    *,
    session_id: str,
    package: FrontierPlanningRequestPackage,
    authorized_max_spend_usd: float,
    frontier_coder_provider: InstrumentedModelProvider | None = None,
) -> tuple[DiscoverySessionRecord, float]:
    """Makes exactly one real, explicitly authorized, spend-ceilinged
    frontier API call for the given package, then applies its response
    through the same ``apply_frontier_planning_response`` the manual
    transport uses. ``authorized_max_spend_usd`` is a hard ceiling on this
    one call, checked both before (a pessimistic worst-case estimate,
    reusing ``evaluation.spend_ceiling`` exactly -- no duplicated
    ceiling-enforcement logic) and after (the real recorded cost) --
    raising ``HostedSpendCeilingExceededError`` and making no state change
    if breached either time. Real, measured token/cache/latency/cost
    telemetry is persisted as an audit artifact; this transport never
    reports ``unmeasured``, unlike the manual transport.
    """

    root_path = Path(root).resolve()
    provider_config = config.models.frontier_coder
    if provider_config is None:
        raise FrontierPlanningApiNotConfiguredError(
            "the API frontier planning transport requires [models.frontier_coder] "
            "to be configured"
        )
    provider = frontier_coder_provider or _build_provider(provider_config)
    ledger = SpendLedger(ceiling_usd=authorized_max_spend_usd)
    spend_ceiling_provider = SpendCeilingModelProvider(
        provider, ledger, default_max_output_tokens=provider_config.max_output_tokens
    )

    prompt = build_frontier_planning_markdown(package)
    invocation = ProviderInvocation(
        request_id=f"MRQ-{uuid.uuid4().hex}",
        operation=ModelOperation.PROPOSE_ARCHITECTURE_PLAN,
        prompt=prompt,
        role=ModelRole.FRONTIER_PLANNING_MODEL,
        response_schema=FrontierPlanningResponseEnvelope.model_json_schema(),
        max_output_tokens=provider_config.max_output_tokens,
        timeout_seconds=provider_config.timeout_seconds,
    )
    audit = DiscoveryAuditStore(root_path, session_id)
    audit.write_text(
        f"frontier-api-prompt-{package.package_id}.txt", prompt, kind="frontier_planning_prompt"
    )
    try:
        call = spend_ceiling_provider.complete(invocation)
    except HostedSpendCeilingExceededError:
        raise
    except InstrumentedProviderError as exc:
        audit.write_json(
            f"frontier-api-telemetry-{package.package_id}.json",
            exc.telemetry,
            kind="provider_telemetry",
        )
        raise DiscoveryError(f"frontier planning API call failed: {exc}") from exc
    audit.write_text(
        f"frontier-api-response-{package.package_id}.txt",
        call.output.content,
        kind="frontier_planning_response_text",
    )
    audit.write_json(
        f"frontier-api-telemetry-{package.package_id}.json", call.telemetry, kind="provider_telemetry"
    )

    try:
        raw_payload = json.loads(call.output.content)
    except json.JSONDecodeError as exc:
        raise MalformedResponseError(f"frontier API response is not valid JSON: {exc}") from exc
    if not isinstance(raw_payload, dict):
        raise MalformedResponseError("frontier API response must be a single JSON object")
    try:
        envelope = FrontierPlanningResponseEnvelope.model_validate(raw_payload)
    except ValidationError as exc:
        raise MalformedResponseError(f"frontier API response failed schema validation: {exc}") from exc

    if envelope.package_id != package.package_id:
        raise ResponseHashMismatchError(
            f"response package_id {envelope.package_id!r} does not match "
            f"the requested package {package.package_id!r}"
        )
    if envelope.package_sha256 != package.package_sha256:
        raise ResponseHashMismatchError(
            "response package_sha256 does not match the package's own hash"
        )

    session = discovery_store.get_session(session_id)
    record = apply_frontier_planning_response(
        root_path, discovery_store, plan_store, config, session, package, envelope, raw_payload
    )
    return record, call.telemetry.estimated_cost_usd


__all__ = [
    "FrontierPlanningApiNotConfiguredError",
    "FrontierPlanningApiPreview",
    "preview_frontier_planning_api_call",
    "run_frontier_planning_api_call",
]

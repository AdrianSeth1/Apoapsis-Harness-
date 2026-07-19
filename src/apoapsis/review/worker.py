from __future__ import annotations

import queue
import threading
from pathlib import Path

from apoapsis.config import ApoapsisConfig, FrontierProviderConfig
from apoapsis.models.frontier import OpenAICompatibleFrontierProvider
from apoapsis.models.local import OllamaProvider
from apoapsis.models.telemetry import InstrumentedModelProvider
from apoapsis.review.errors import ReviewError
from apoapsis.review.execution import run_review_operation
from apoapsis.review.schema import ContinuationBudget, ReviewActionKind, ReviewCase
from apoapsis.review.store import ReviewOperationStore
from apoapsis.workflow.engine import SQLiteTaskStore


def _build_provider(provider_config: FrontierProviderConfig) -> InstrumentedModelProvider:
    if provider_config.provider == "ollama":
        adapter = OllamaProvider(provider_config)
    elif provider_config.provider == "openai_compatible":
        adapter = OpenAICompatibleFrontierProvider(provider_config)
    else:
        raise ReviewError(f"unsupported provider: {provider_config.provider}")
    return InstrumentedModelProvider(adapter, provider_config.pricing)


class ReviewWorker:
    """Runs authorized human-review operations on a background thread,
    outside any HTTP request (ADR 0020 Commit C2).

    Submission (``prepare_review_operation``, called synchronously by the
    HTTP handler before ``submit()``) has already validated the request and
    durably recorded the operation as ``RECORDED`` before this worker ever
    sees it -- a browser disconnect after that point cannot cancel,
    duplicate, or repeat the operation; it just keeps running and its
    result is read back later by polling the operation id. If the server
    process itself is killed while a job is queued or running, the
    operation is left ``RECORDED``/``RUNNING`` in the durable store exactly
    like a CLI crash would leave it -- the same fail-closed behavior
    documented in ADR 0020, not a new failure mode this worker introduces.
    """

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._queue: queue.Queue[
            tuple[ReviewCase, ReviewActionKind, str, int, ContinuationBudget | None]
        ] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(
        self,
        review_case: ReviewCase,
        *,
        action: ReviewActionKind,
        operation_id: str,
        expected_version: int,
        budget: ContinuationBudget | None,
    ) -> None:
        self._queue.put((review_case, action, operation_id, expected_version, budget))

    def _run(self) -> None:
        while True:
            review_case, action, operation_id, expected_version, budget = (
                self._queue.get()
            )
            try:
                self._execute(review_case, action, operation_id, expected_version, budget)
            except Exception:
                # `run_review_operation` already records failure on the
                # operation itself; this worker loop must never die.
                pass

    def _execute(
        self,
        review_case: ReviewCase,
        action: ReviewActionKind,
        operation_id: str,
        expected_version: int,
        budget: ContinuationBudget | None,
    ) -> None:
        config = ApoapsisConfig.from_toml(
            self.project_root / ".apoapsis" / "config.toml"
        )
        task_store = SQLiteTaskStore(self.project_root / ".apoapsis" / "apoapsis.db")
        operation_store = ReviewOperationStore(
            self.project_root / ".apoapsis" / "review-operations.db"
        )
        local_provider = None
        frontier_provider = None
        if action == ReviewActionKind.LOCAL_CONTINUATION:
            local_config = config.models.local_coder or config.models.frontier
            local_provider = _build_provider(local_config)
        elif action == ReviewActionKind.FRONTIER_CONTINUATION:
            assert config.models.frontier_coder is not None
            frontier_provider = _build_provider(config.models.frontier_coder)
        run_review_operation(
            self.project_root,
            task_store,
            operation_store,
            config,
            review_case,
            action=action,
            operation_id=operation_id,
            expected_version=expected_version,
            budget=budget,
            local_coder_provider=local_provider,
            frontier_coder_provider=frontier_provider,
        )


__all__ = ["ReviewWorker"]

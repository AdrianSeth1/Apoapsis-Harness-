from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apoapsis.desktop.errors import (
    CapabilitySessionError,
    ImportSafetyError,
    ReferenceEvidenceSafetyError,
    ReferenceProjectInvalidError,
)
from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.safety import hard_exclusion_reason, looks_binary, resolve_within_root
from apoapsis.desktop.schema import ReferenceEvidenceRecord, ReferenceProjectRecord
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import utc_now

_SNIFF_BYTES = 8192
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _ReferenceBinding:
    primary_root: Path
    reference_root: Path


class DesktopReferenceService:
    """Read-only cross-project reference attachment (ADR 0050 Phase 4,
    ADR 0052). "Attach reference project" is deliberately a third,
    distinct operation from "Open project" and "Import files" -- it never
    makes the reference project writable, never lets a model browse it
    freely, and never expands where a patch may land. The operator
    explicitly selects which files become evidence one at a time; every
    captured piece of evidence records its exact source project, commit,
    relative path, and content hash, so it can never quietly become
    unattributed context later.
    """

    def __init__(self, project_service: DesktopProjectService) -> None:
        self._project_service = project_service
        self._bindings: dict[str, _ReferenceBinding] = {}
        self._lock = threading.Lock()

    def attach_reference_project(
        self, session_id: str, reference_path: str | Path
    ) -> dict[str, Any]:
        """Binds a new opaque `reference_session_id` to
        `(primary_root, reference_root)`. Read-only from this point on --
        nothing in this class ever opens a file in the reference project
        for writing, and no Git command run against it is anything but a
        status read."""

        primary_root = self._project_service.resolve_session(session_id)
        reference_root = Path(reference_path).resolve(strict=False)

        if not reference_root.is_dir():
            raise ReferenceProjectInvalidError(
                f"reference path is not a directory: {reference_root}"
            )
        if not (reference_root / ".git").exists():
            raise ReferenceProjectInvalidError(
                f"reference path is not a Git repository: {reference_root}"
            )
        if reference_root == primary_root:
            raise ReferenceProjectInvalidError(
                "a project cannot be attached as its own reference project"
            )
        if self._is_nested(primary_root, reference_root) or self._is_nested(
            reference_root, primary_root
        ):
            raise ReferenceProjectInvalidError(
                "the reference project must not be nested inside, or "
                "contain, the primary project"
            )

        snapshot = GitRepository(reference_root).snapshot()
        reference_session_id = f"desktop-reference-{secrets.token_urlsafe(24)}"
        with self._lock:
            self._bindings[reference_session_id] = _ReferenceBinding(
                primary_root=primary_root, reference_root=reference_root
            )

        record = ReferenceProjectRecord(
            reference_session_id=reference_session_id,
            primary_canonical_path=str(primary_root),
            reference_canonical_path=str(reference_root),
            display_name=reference_root.name or str(reference_root),
            attached_at=utc_now(),
            head_commit=snapshot.head_commit,
            branch=snapshot.branch,
            is_clean=snapshot.is_clean,
        )
        return record.model_dump(mode="json")

    def detach_reference_project(self, reference_session_id: str) -> dict[str, Any]:
        """Revokes *future* access through this reference session. Evidence
        already captured under the primary project's own
        `.apoapsis/reference-evidence/` is left in place -- it is a
        durable, provenance-bound record of a past, explicit decision, not
        a live handle for this call to revoke."""

        with self._lock:
            existed = self._bindings.pop(reference_session_id, None) is not None
        return {"reference_session_id": reference_session_id, "detached": existed}

    def select_reference_evidence(
        self, reference_session_id: str, relative_paths: list[str]
    ) -> dict[str, Any]:
        """The operator's explicit, one-file-at-a-time selection of what
        becomes reference evidence. Nothing is captured merely because the
        reference project was attached -- this call is the only path from
        "attached" to "readable evidence", and it never accepts a
        directory (the operator selects files, not a subtree to sweep in
        wholesale, which is what the *import* workflow is for -- and
        import copies into the tracked project; this never does)."""

        binding = self._resolve_binding(reference_session_id)
        snapshot = GitRepository(binding.reference_root).snapshot()

        evidence: list[ReferenceEvidenceRecord] = []
        for relative_path in relative_paths:
            try:
                source_path = resolve_within_root(binding.reference_root, relative_path)
            except ImportSafetyError as exc:
                raise ReferenceEvidenceSafetyError(str(exc)) from exc

            if not source_path.exists():
                raise ReferenceEvidenceSafetyError(
                    f"reference evidence source does not exist: {relative_path}"
                )
            if source_path.is_symlink():
                raise ReferenceEvidenceSafetyError(
                    f"reference evidence must not be a symlink: {relative_path}"
                )
            if source_path.is_dir():
                raise ReferenceEvidenceSafetyError(
                    "reference evidence must be a single file, not a "
                    f"directory: {relative_path}"
                )
            exclusion_reason = hard_exclusion_reason(relative_path)
            if exclusion_reason is not None:
                raise ReferenceEvidenceSafetyError(
                    f"excluded from reference evidence ({exclusion_reason}): "
                    f"{relative_path}"
                )

            sha256 = self._hash_file(source_path)
            size_bytes = source_path.stat().st_size
            is_binary = self._sniff_binary(source_path)

            cached_path = (
                binding.primary_root
                / ".apoapsis"
                / "reference-evidence"
                / reference_session_id
                / relative_path
            )
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            cached_path.write_bytes(source_path.read_bytes())

            record = ReferenceEvidenceRecord(
                reference_session_id=reference_session_id,
                source_canonical_path=str(binding.reference_root),
                source_commit=snapshot.head_commit,
                relative_path=relative_path,
                sha256=sha256,
                size_bytes=size_bytes,
                is_binary=is_binary,
                cached_path=str(cached_path),
                captured_at=utc_now(),
            )
            evidence.append(record)
            self._append_ledger(binding.primary_root, reference_session_id, record)

        return {"evidence": [item.model_dump(mode="json") for item in evidence]}

    def list_reference_evidence(self, session_id: str) -> dict[str, Any]:
        """All evidence ever captured for the primary project bound to
        `session_id`, across every reference session -- read straight
        from the append-only on-disk ledger, not from in-memory state, so
        it survives a process restart even though the capability sessions
        themselves do not."""

        primary_root = self._project_service.resolve_session(session_id)
        ledger_root = primary_root / ".apoapsis" / "reference-evidence"
        records: list[dict[str, Any]] = []
        if ledger_root.is_dir():
            for ledger_file in sorted(ledger_root.glob("*/evidence.jsonl")):
                for line in ledger_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        records.append(json.loads(line))
        return {"evidence": records}

    # -- internals --------------------------------------------------------

    def _resolve_binding(self, reference_session_id: str) -> _ReferenceBinding:
        with self._lock:
            binding = self._bindings.get(reference_session_id)
        if binding is None:
            raise CapabilitySessionError(
                f"unknown or detached reference session: {reference_session_id!r}"
            )
        return binding

    @staticmethod
    def _is_nested(outer: Path, inner: Path) -> bool:
        try:
            inner.relative_to(outer)
        except ValueError:
            return False
        return inner != outer

    @staticmethod
    def _append_ledger(
        primary_root: Path, reference_session_id: str, record: ReferenceEvidenceRecord
    ) -> None:
        ledger_dir = (
            primary_root / ".apoapsis" / "reference-evidence" / reference_session_id
        )
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = ledger_dir / "evidence.jsonl"
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sniff_binary(path: Path) -> bool:
        with path.open("rb") as handle:
            sample = handle.read(_SNIFF_BYTES)
        return looks_binary(sample)


__all__ = ["DesktopReferenceService"]

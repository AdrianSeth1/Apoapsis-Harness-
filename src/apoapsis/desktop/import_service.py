from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from apoapsis.desktop.errors import (
    ImportApprovalError,
    ImportPreviewNotFoundError,
    ImportSafetyError,
)
from apoapsis.desktop.project_service import DesktopProjectService
from apoapsis.desktop.safety import (
    hard_exclusion_reason,
    is_safe_destination_relative_path,
    looks_binary,
    resolve_within_root,
)
from apoapsis.desktop.schema import (
    ImportDecision,
    ImportFileDisposition,
    ImportFileEntry,
    ImportLimits,
    ImportManifest,
    ImportPreview,
)
from apoapsis.repository.git import GitRepository
from apoapsis.specification.schema import utc_now

_HASH_CHUNK_BYTES = 1024 * 1024
_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class _CandidateFile:
    absolute_source: Path
    relative_destination: str
    is_symlink: bool
    #: What the exclusion rules are evaluated against.
    #:
    #: Not the same string as `relative_destination`, and that difference is a
    #: fixed defect rather than a nicety. An explicitly named source file is
    #: destined for `<dest>/<basename>`, so a file chosen from inside `.git`
    #: arrived at the exclusion check as `HEAD` -- the `.git` parent had already
    #: been dropped, and the directory exclusion could not see it. Walked
    #: directories were pruned correctly, so the hole opened only for the case
    #: an operator is most likely to hit: picking the file directly.
    exclusion_probe: str = ""

    def probe(self) -> str:
        return self.exclusion_probe or self.relative_destination


class _PreviewState:
    __slots__ = ("preview", "project_root", "decision")

    def __init__(self, preview: ImportPreview, project_root: Path) -> None:
        self.preview = preview
        self.project_root = project_root
        self.decision: ImportDecision | None = None


class DesktopImportService:
    """Deterministic, previewed, staged file/folder import (ADR 0050 Phase
    3, ADR 0051). Never called by a model. Every source path must already
    exist on disk (it came from wherever a native picker's result arrives);
    every destination is resolved and contained by `resolve_within_root`
    before anything is read or copied.

    Preview -> approve -> execute are three separate calls, matching ADR
    0050 Phase 6's typed-API names exactly. Preview state is held in
    memory only (like `ProjectCapabilitySessions`, it does not need to
    survive a process restart); the *executed* import's manifest is the
    durable, on-disk audit artifact.
    """

    def __init__(
        self,
        project_service: DesktopProjectService,
        *,
        limits: ImportLimits | None = None,
    ) -> None:
        self._project_service = project_service
        self._limits = limits or ImportLimits()
        self._previews: dict[str, _PreviewState] = {}
        self._lock = threading.Lock()

    # -- preview --------------------------------------------------------

    def preview_import(
        self,
        session_id: str,
        *,
        sources: list[str],
        destination_relative_dir: str = "",
    ) -> dict[str, Any]:
        project_root = self._project_service.resolve_session(session_id)

        # Validated BEFORE normalisation, which is the whole fix. This used to
        # `.strip("/")` first, so `/etc` became `etc` and then passed a
        # validator whose first rule is `startswith("/") -> False`. The
        # normalisation destroyed the exact property the check exists to test,
        # and the absolute-destination test has been failing ever since.
        if destination_relative_dir and not is_safe_destination_relative_path(
            destination_relative_dir
        ):
            raise ImportSafetyError(
                f"unsafe destination directory: {destination_relative_dir!r}"
            )
        # Trailing separators are cosmetic and are still normalised away; a
        # leading one is not cosmetic and no longer reaches this line.
        destination_relative_dir = destination_relative_dir.rstrip("/")
        # Validates containment even for the "no subdirectory" case, so a
        # later per-file join can never be the first containment check.
        # Resolved only to validate containment and to record in the
        # preview -- nothing is created on disk during a preview.
        destination_root = (
            project_root
            if not destination_relative_dir
            else resolve_within_root(project_root, destination_relative_dir)
        )

        candidates = self._enumerate_candidates(sources, destination_relative_dir)

        entries: list[ImportFileEntry] = []
        total_bytes_to_copy = 0
        new_count = replacement_count = conflict_count = skipped_count = 0

        for candidate in candidates:
            destination_path = resolve_within_root(
                project_root, candidate.relative_destination
            )
            destination_full_path = str(destination_path)

            if candidate.is_symlink:
                entries.append(
                    ImportFileEntry(
                        source_path=str(candidate.absolute_source),
                        relative_destination_path=candidate.relative_destination,
                        destination_path=destination_full_path,
                        disposition=ImportFileDisposition.SKIPPED_SYMLINK,
                        reason="symlinks and junctions are not followed by default",
                    )
                )
                skipped_count += 1
                continue

            exclusion_reason = hard_exclusion_reason(candidate.probe())
            if exclusion_reason is not None:
                entries.append(
                    ImportFileEntry(
                        source_path=str(candidate.absolute_source),
                        relative_destination_path=candidate.relative_destination,
                        destination_path=destination_full_path,
                        disposition=ImportFileDisposition.SKIPPED_EXCLUDED,
                        reason=exclusion_reason,
                    )
                )
                skipped_count += 1
                continue

            disposition, reason = self._classify_destination(destination_path)
            size_bytes = candidate.absolute_source.stat().st_size
            sha256 = self._hash_file(candidate.absolute_source)
            is_binary = self._sniff_binary(candidate.absolute_source)

            entries.append(
                ImportFileEntry(
                    source_path=str(candidate.absolute_source),
                    relative_destination_path=candidate.relative_destination,
                    destination_path=destination_full_path,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    is_binary=is_binary,
                    disposition=disposition,
                    reason=reason,
                )
            )
            if disposition == ImportFileDisposition.CONFLICT:
                conflict_count += 1
                continue
            if disposition == ImportFileDisposition.NEW:
                new_count += 1
            elif disposition == ImportFileDisposition.REPLACEMENT:
                replacement_count += 1
            total_bytes_to_copy += size_bytes

        total_files_to_copy = new_count + replacement_count
        destination_repository_clean = GitRepository(project_root).snapshot().is_clean

        preview = ImportPreview(
            preview_id=f"IMPPREV-{secrets.token_hex(10)}",
            session_id=session_id,
            source_root=self._common_source_root(candidates),
            destination_root=str(destination_root),
            destination_relative_dir=destination_relative_dir,
            created_at=utc_now(),
            entries=entries,
            total_files_considered=len(entries),
            total_files_to_copy=total_files_to_copy,
            total_bytes_to_copy=total_bytes_to_copy,
            new_file_count=new_count,
            replacement_count=replacement_count,
            conflict_count=conflict_count,
            skipped_count=skipped_count,
            destination_repository_clean=destination_repository_clean,
            exceeds_file_limit=total_files_to_copy > self._limits.max_files,
            exceeds_byte_limit=total_bytes_to_copy > self._limits.max_total_bytes,
            requires_replacement_confirmation=replacement_count > 0,
        )
        with self._lock:
            self._previews[preview.preview_id] = _PreviewState(preview, project_root)
        return preview.model_dump(mode="json")

    # -- approve ----------------------------------------------------------

    def approve_import(
        self,
        session_id: str,
        preview_id: str,
        *,
        replacements_confirmed: bool = False,
    ) -> dict[str, Any]:
        state = self._get_preview_state(session_id, preview_id)
        if (
            state.preview.requires_replacement_confirmation
            and not replacements_confirmed
        ):
            raise ImportApprovalError(
                "this import replaces existing files; "
                "replacements_confirmed=True is required"
            )
        if state.preview.conflict_count > 0:
            raise ImportApprovalError(
                "this import has unresolved conflicts and cannot be approved "
                "as-is; choose a different destination"
            )
        decision = ImportDecision(
            preview_id=preview_id,
            approved=True,
            replacements_confirmed=replacements_confirmed,
            decided_at=utc_now(),
        )
        with self._lock:
            state.decision = decision
        return decision.model_dump(mode="json")

    # -- execute ------------------------------------------------------------

    def execute_import(self, session_id: str, preview_id: str) -> dict[str, Any]:
        state = self._get_preview_state(session_id, preview_id)
        if state.decision is None or not state.decision.approved:
            raise ImportApprovalError(
                f"preview {preview_id!r} has not been approved via approve_import"
            )

        project_root = state.project_root
        import_id = f"IMPORT-{secrets.token_hex(10)}"
        staging_dir = project_root / ".apoapsis" / "import-staging" / import_id
        backups_dir = project_root / ".apoapsis" / "import-backups" / import_id
        manifests_dir = project_root / ".apoapsis" / "import-manifests"

        copyable = [
            entry
            for entry in state.preview.entries
            if entry.disposition
            in (ImportFileDisposition.NEW, ImportFileDisposition.REPLACEMENT)
        ]

        staging_dir.mkdir(parents=True, exist_ok=True)
        staged_paths: dict[str, Path] = {}
        try:
            for entry in copyable:
                source = Path(entry.source_path)
                if not source.is_file() or source.is_symlink():
                    raise ImportSafetyError(
                        f"source changed since preview and is no longer a "
                        f"plain file: {entry.source_path}"
                    )
                if self._hash_file(source) != entry.sha256:
                    raise ImportSafetyError(
                        f"source content changed since preview: {entry.source_path}"
                    )
                staged_path = staging_dir / entry.relative_destination_path
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, staged_path)
                staged_paths[entry.relative_destination_path] = staged_path

            backup_paths: dict[str, str] = {}
            for entry in copyable:
                if entry.disposition != ImportFileDisposition.REPLACEMENT:
                    continue
                destination = Path(entry.destination_path)
                if destination.exists():
                    backup_path = backups_dir / entry.relative_destination_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup_path)
                    backup_paths[entry.relative_destination_path] = str(backup_path)

            copied_relative_paths: list[str] = []
            for entry in copyable:
                destination = Path(entry.destination_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged_path = staged_paths[entry.relative_destination_path]
                os.replace(staged_path, destination)
                copied_relative_paths.append(entry.relative_destination_path)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

        skipped_relative_paths = [
            entry.relative_destination_path
            for entry in state.preview.entries
            if entry.disposition
            in (
                ImportFileDisposition.SKIPPED_EXCLUDED,
                ImportFileDisposition.SKIPPED_SYMLINK,
            )
        ]
        conflict_relative_paths = [
            entry.relative_destination_path
            for entry in state.preview.entries
            if entry.disposition == ImportFileDisposition.CONFLICT
        ]

        manifest = ImportManifest(
            import_id=import_id,
            preview=state.preview,
            decision=state.decision,
            copied_relative_paths=copied_relative_paths,
            skipped_relative_paths=skipped_relative_paths,
            conflict_relative_paths=conflict_relative_paths,
            backup_paths=backup_paths,
            executed_at=utc_now(),
        )
        manifests_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifests_dir / f"{import_id}.json"
        manifest_path.write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )

        with self._lock:
            self._previews.pop(preview_id, None)

        payload = manifest.model_dump(mode="json")
        payload["manifest_path"] = str(manifest_path)
        return payload

    # -- internals --------------------------------------------------------

    def _get_preview_state(self, session_id: str, preview_id: str) -> _PreviewState:
        project_root = self._project_service.resolve_session(session_id)
        with self._lock:
            state = self._previews.get(preview_id)
        if state is None or state.preview.session_id != session_id:
            raise ImportPreviewNotFoundError(
                f"no active import preview {preview_id!r} for this session"
            )
        if state.project_root != project_root:
            # A session id is only ever bound to one project root for its
            # lifetime, so this should be unreachable -- fail closed rather
            # than silently importing against the wrong project.
            raise ImportPreviewNotFoundError(
                f"preview {preview_id!r} does not belong to the current project"
            )
        return state

    @staticmethod
    def _enumerate_candidates(
        sources: list[str], destination_relative_dir: str
    ) -> list[_CandidateFile]:
        candidates: list[_CandidateFile] = []
        for raw_source in sources:
            source_path = Path(raw_source)
            if not source_path.exists():
                raise ImportSafetyError(f"source does not exist: {raw_source}")
            if source_path.is_symlink():
                candidates.append(
                    _CandidateFile(
                        absolute_source=source_path,
                        relative_destination=DesktopImportService._join_destination(
                            destination_relative_dir, source_path.name
                        ),
                        is_symlink=True,
                    )
                )
            elif source_path.is_dir():
                candidates.extend(
                    DesktopImportService._walk_directory(
                        source_path, destination_relative_dir
                    )
                )
            else:
                candidates.append(
                    _CandidateFile(
                        absolute_source=source_path,
                        relative_destination=DesktopImportService._join_destination(
                            destination_relative_dir, source_path.name
                        ),
                        is_symlink=False,
                        # The immediate parent, so a file selected from inside
                        # `.git`, `.apoapsis`, `node_modules` or a build
                        # directory is still recognised as coming from one. Only
                        # the immediate parent: walking further up would start
                        # matching the operator's own directory names, which
                        # have nothing to do with what is being imported.
                        exclusion_probe=DesktopImportService._exclusion_probe(
                            source_path
                        ),
                    )
                )
        return candidates

    @staticmethod
    def _exclusion_probe(source_path: Path) -> str:
        parent = source_path.parent.name
        return f"{parent}/{source_path.name}" if parent else source_path.name

    @staticmethod
    def _walk_directory(
        source_dir: Path, destination_relative_dir: str
    ) -> list[_CandidateFile]:
        results: list[_CandidateFile] = []
        for dirpath_str, dirnames, filenames in os.walk(source_dir, followlinks=False):
            dirpath = Path(dirpath_str)
            # Never descend into a symlinked/junction subdirectory, and
            # never descend into an always-excluded directory at all --
            # both a correctness guard (symlinks) and an efficiency guard
            # (a huge node_modules/.git tree is never walked file-by-file
            # just to discover every entry is excluded anyway).
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not (dirpath / name).is_symlink()
                and hard_exclusion_reason(name) is None
            )
            for filename in sorted(filenames):
                file_path = dirpath / filename
                relative_to_source = file_path.relative_to(source_dir.parent).as_posix()
                results.append(
                    _CandidateFile(
                        absolute_source=file_path,
                        relative_destination=DesktopImportService._join_destination(
                            destination_relative_dir, relative_to_source
                        ),
                        is_symlink=file_path.is_symlink(),
                    )
                )
        return results

    @staticmethod
    def _common_source_root(candidates: list[_CandidateFile]) -> str:
        if not candidates:
            return ""
        try:
            return os.path.commonpath([str(c.absolute_source) for c in candidates])
        except ValueError:
            # Sources spanned multiple drives/roots (Windows) -- no single
            # common root exists; record an empty string rather than
            # guessing one.
            return ""

    @staticmethod
    def _join_destination(destination_relative_dir: str, tail: str) -> str:
        tail_posix = PurePosixPath(tail).as_posix()
        if not destination_relative_dir:
            return tail_posix
        return f"{destination_relative_dir}/{tail_posix}"

    @staticmethod
    def _classify_destination(destination_path: Path) -> tuple[str, str | None]:
        if not destination_path.exists():
            return ImportFileDisposition.NEW, None
        if destination_path.is_dir():
            return (
                ImportFileDisposition.CONFLICT,
                "a directory already exists at this destination path",
            )
        return ImportFileDisposition.REPLACEMENT, None

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


__all__ = ["DesktopImportService"]

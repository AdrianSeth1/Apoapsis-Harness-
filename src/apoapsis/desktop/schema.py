from __future__ import annotations

from datetime import datetime

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class ProjectStatus:
    """Deterministic status values `DesktopProjectService.validate_project`
    can return. Not a Python `Enum` so it round-trips through
    `model_dump(mode="json")` as a plain string without an extra decode
    step, matching this codebase's existing status-string convention
    (e.g. `WorkflowState`, `DiscoveryStatus` values)."""

    OK = "ok"
    MISSING = "missing"
    INACCESSIBLE = "inaccessible"
    NOT_GIT_REPOSITORY = "not_git_repository"
    NOT_INITIALIZED = "not_initialized"


class ProjectValidation(StrictModel):
    """The result of deterministically inspecting one canonical path.
    Never invented from a model's claim -- every field is read directly
    from the filesystem by `DesktopProjectService.validate_project`."""

    canonical_path: str
    exists: bool
    is_directory: bool
    is_git_repository: bool
    is_initialized: bool
    status: str
    detail: str


class ProjectRecord(StrictModel):
    """One row of the application-owned project registry (ADR 0051).
    Contains only a canonical path and harmless display metadata -- no
    credentials, no repository contents, nothing model-visible."""

    canonical_path: str
    display_name: str
    added_at: datetime
    last_opened_at: datetime
    initialized: bool = False


class ImportFileDisposition:
    """Deterministic per-file outcome categories shown in an import
    preview and recorded in the executed import's manifest."""

    NEW = "new"
    REPLACEMENT = "replacement"
    CONFLICT = "conflict"
    SKIPPED_EXCLUDED = "skipped_excluded"
    SKIPPED_SYMLINK = "skipped_symlink"


class ImportFileEntry(StrictModel):
    """One source file considered for import. `sha256`/`is_binary` are
    only populated for files Apoapsis actually intends to copy (`NEW` or
    `REPLACEMENT`) -- excluded/skipped entries are recorded for the
    preview and audit trail without being read."""

    source_path: str
    relative_destination_path: str
    destination_path: str
    size_bytes: int | None = None
    sha256: str | None = None
    is_binary: bool = False
    is_symlink: bool = False
    disposition: str
    reason: str | None = None


class ImportPreview(StrictModel):
    """A deterministic, reproducible preview of one proposed import.
    Nothing here has copied a byte yet -- `preview_import` only reads
    metadata and hashes candidate source files."""

    preview_id: str
    session_id: str
    source_root: str
    destination_root: str
    destination_relative_dir: str
    created_at: datetime
    entries: list[ImportFileEntry] = Field(default_factory=list)
    total_files_considered: int = 0
    total_files_to_copy: int = 0
    total_bytes_to_copy: int = 0
    new_file_count: int = 0
    replacement_count: int = 0
    conflict_count: int = 0
    skipped_count: int = 0
    destination_repository_clean: bool = True
    exceeds_file_limit: bool = False
    exceeds_byte_limit: bool = False
    requires_replacement_confirmation: bool = False


class ImportDecision(StrictModel):
    """The operator's explicit approval of one preview -- required before
    `execute_import` will copy anything."""

    preview_id: str
    approved: bool
    replacements_confirmed: bool
    decided_at: datetime


class ImportManifest(StrictModel):
    """The durable, append-only audit artifact written for every executed
    import (ADR 0051), under the project's own `.apoapsis/import-manifests/`
    directory -- never inside a shared/global location, and never deleted
    by later imports."""

    import_id: str
    preview: ImportPreview
    decision: ImportDecision
    copied_relative_paths: list[str] = Field(default_factory=list)
    skipped_relative_paths: list[str] = Field(default_factory=list)
    conflict_relative_paths: list[str] = Field(default_factory=list)
    backup_paths: dict[str, str] = Field(default_factory=dict)
    executed_at: datetime


class ReferenceProjectRecord(StrictModel):
    """The result of attaching one read-only reference project (ADR 0050
    Phase 4, ADR 0052) to a primary project's window session. Captured
    once, at attach time -- `select_reference_evidence` re-reads the
    reference repository's live state itself rather than trusting this
    snapshot to still be current."""

    reference_session_id: str
    primary_canonical_path: str
    reference_canonical_path: str
    display_name: str
    attached_at: datetime
    head_commit: str | None = None
    branch: str | None = None
    is_clean: bool = True


class ReferenceEvidenceRecord(StrictModel):
    """One piece of explicitly user-selected, provenance-bound evidence
    copied read-only from an attached reference project into the primary
    project's own `.apoapsis/reference-evidence/` cache. Never written
    into the primary project's tracked source, never executed, and never
    handed to a model except as sanitized, attributed context -- exactly
    like Research Mode's evidence model."""

    reference_session_id: str
    source_canonical_path: str
    source_commit: str | None
    relative_path: str
    sha256: str
    size_bytes: int
    is_binary: bool
    cached_path: str
    captured_at: datetime


class ImportLimits(StrictModel):
    """Configured ceilings an import preview checks itself against. Not
    (yet) read from `.apoapsis/config.toml` -- see ADR 0051's non-goals --
    but already isolated behind a typed model so wiring that in later is
    additive, not a redesign."""

    max_files: int = Field(default=2000, ge=1)
    max_total_bytes: int = Field(default=200_000_000, ge=1)

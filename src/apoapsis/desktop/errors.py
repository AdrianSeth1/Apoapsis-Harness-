from __future__ import annotations


class DesktopError(RuntimeError):
    """Base error for every desktop project-management operation."""


class ProjectNotFoundError(DesktopError):
    """Raised when a canonical project path does not exist or is not a
    directory -- covers moved and missing projects."""


class ProjectNotGitRepositoryError(DesktopError):
    """Raised when a selected directory has no `.git` -- Apoapsis never
    creates a repository for the operator."""


class ProjectAlreadyInitializedError(DesktopError):
    """Raised when initialization is requested for a project that already
    has `.apoapsis/config.toml`."""


class ProjectNotInitializedError(DesktopError):
    """Raised when an operation requires an initialized project (one with
    `.apoapsis/config.toml`) and the selected project does not have one."""


class RegistryStoreError(DesktopError):
    """Raised for project-registry persistence failures."""


class CapabilitySessionError(DesktopError):
    """Raised when an opaque window/project capability session id is
    unknown, expired (process restarted), or otherwise cannot be resolved
    to a project root. Never raised with a path the caller supplied --
    only ever with the opaque id itself."""


class ImportSafetyError(DesktopError):
    """Raised when a proposed import violates a hard safety rule: a
    traversal attempt, an absolute destination path, a reserved device
    name, a destination that would escape the project root, or a source
    that no longer exists."""


class ImportPreviewNotFoundError(DesktopError):
    """Raised when an import preview id is unknown, belongs to a different
    project session, or has already been executed."""


class ImportApprovalError(DesktopError):
    """Raised when `execute_import` is requested without a prior, matching
    `approve_import` decision, or when a preview containing replacements is
    approved without the required second confirmation."""


class ReferenceProjectInvalidError(DesktopError):
    """Raised when a proposed reference project is not a Git repository,
    is the same canonical path as the primary project, or is nested inside
    (or contains) the primary project -- read-only attachment must not be
    allowed to create a confusing or self-referential binding."""


class ReferenceEvidenceSafetyError(DesktopError):
    """Raised when a selected reference-evidence path is excluded (secret-
    like, `.git`/`.apoapsis`/`.sol`, a dependency/build directory), is a
    symlink, escapes the reference project root, or does not exist."""

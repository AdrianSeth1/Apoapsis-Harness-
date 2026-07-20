from __future__ import annotations


class ManualFrontierError(RuntimeError):
    """Base error for every manual subscription-frontier handoff operation."""


class PackageNotFoundError(ManualFrontierError):
    """Raised when a referenced handoff package cannot be found on disk."""


class PackageIntegrityError(ManualFrontierError):
    """Raised when a package's on-disk content no longer matches its own
    recorded hash -- fail closed rather than trust a possibly-tampered
    package file."""


class TaskVersionMismatchError(ManualFrontierError):
    """Raised when the task has moved to a different version since the
    package was exported or the response was imported."""


class WorktreeFingerprintMismatchError(ManualFrontierError):
    """Raised when the current worktree no longer matches the fingerprint
    a package or preview was built against."""


class EligibilityError(ManualFrontierError):
    """Raised when manual-frontier handoff is not currently an eligible
    action for this task (wrong state, wrong stop reason, or the
    configured repair-round ceiling has already been reached)."""


class ResponseTooLargeError(ManualFrontierError):
    """Raised when a pasted response exceeds the configured byte ceiling,
    checked before any JSON parsing is attempted."""


class MalformedResponseError(ManualFrontierError):
    """Raised when a pasted response is not valid JSON or does not match
    the strict ``ManualFrontierResponseEnvelope`` schema."""


class ResponseHashMismatchError(ManualFrontierError):
    """Raised when a response's declared ``package_id``/``package_sha256``
    does not match the package it claims to answer."""


class PatchParseError(ManualFrontierError):
    """Raised when the response's patch is not a well-formed unified diff."""


class PatchPolicyRejectedError(ManualFrontierError):
    """Raised when a syntactically valid patch violates deterministic
    repository patch policy (file count, size, protected paths, ...)."""


class PreviewNotFoundError(ManualFrontierError):
    """Raised when a referenced preview id does not exist."""


class PreviewNotApprovedError(ManualFrontierError):
    """Raised when applying a preview is requested before the required
    first approval step has been recorded (two-step approval, ADR 0031)."""


class PreviewStaleError(ManualFrontierError):
    """Raised when a preview's captured task version or worktree
    fingerprint no longer matches current state at apply time."""


class RepairCeilingExceededError(ManualFrontierError):
    """Raised when exporting or applying a manual-frontier handoff would
    exceed the configured, small, deterministic maximum number of repair
    rounds for this task."""

from __future__ import annotations


class DiscoveryError(RuntimeError):
    """Base error for every discovery-flow operation."""


class SessionNotFoundError(DiscoveryError):
    """Raised when a referenced discovery session does not exist."""


class InvalidTransitionError(DiscoveryError):
    """Raised when a requested action is not valid from the session's
    current status."""


class ConcurrentSessionTransitionError(DiscoveryError):
    """Raised when the session's version no longer matches what was
    expected -- optimistic-versioning conflict."""


class QuestionCeilingExceededError(DiscoveryError):
    """Raised when a model (local or frontier) proposes more clarification
    questions than the configured, harness-enforced ceiling."""


class AnswerMismatchError(DiscoveryError):
    """Raised when submitted answers do not correspond 1:1 to the
    questions they claim to answer."""


class BriefNotApprovedError(DiscoveryError):
    """Raised when an action requires an approved idea brief but none
    exists yet."""


class ClarificationRoundCeilingExceededError(DiscoveryError):
    """Raised when a frontier clarification round would exceed the
    configured, small, deterministic maximum -- this is a bounded planning
    workflow, never an unbounded conversation."""


class PackageNotFoundError(DiscoveryError):
    """Raised when a referenced frontier planning package cannot be found
    on disk."""


class PackageIntegrityError(DiscoveryError):
    """Raised when a package's on-disk content no longer matches its own
    recorded hash."""


class StaleSessionError(DiscoveryError):
    """Raised when a response references a session version, package, or
    round that no longer matches current state -- a stale or replayed
    response is rejected, never silently accepted."""


class ResponseTooLargeError(DiscoveryError):
    """Raised when a pasted response exceeds the configured byte ceiling,
    checked before any JSON parsing is attempted."""


class MalformedResponseError(DiscoveryError):
    """Raised when a pasted response is not valid JSON or does not match
    the strict response envelope schema."""


class ResponseHashMismatchError(DiscoveryError):
    """Raised when a response's declared package id/hash does not match
    the package it claims to answer."""


class PlanQualityError(DiscoveryError):
    """Raised when a returned plan variant fails a basic, deterministic
    well-formedness check before it is even handed to the existing
    Architect Mode import/validation machinery."""

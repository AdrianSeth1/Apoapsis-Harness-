"""Desktop project-management services (ADR 0050, ADR 0051).

This package implements the *trusted* half of the native desktop shell's
project selection and file-import workflow: an application-owned project
registry, in-memory window/project capability sessions, and a deterministic,
staged, previewed file-import service. Every entry point here operates on
canonical filesystem paths that were already resolved server-side -- never
an arbitrary, browser- or model-supplied path string -- and no model ever
calls anything in this package. See `HANDOFF.md`'s authority boundary and
ADR 0050/0051 before changing it.
"""

from __future__ import annotations

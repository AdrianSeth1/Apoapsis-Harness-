"""Architect Mode: deterministic planning foundation (ADR 0019).

Submodules are imported directly (``apoapsis.architect.schema``,
``apoapsis.architect.validation``, ...) rather than re-exported here, to
avoid import cycles between this package and ``apoapsis.config`` /
``apoapsis.context`` / ``apoapsis.verification``.
"""

from __future__ import annotations

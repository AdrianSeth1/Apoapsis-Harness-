"""Keep qualification-package fixtures out of this repository's own suite.

A case package under `docs/qualification/pilot/` contains candidate source
trees written for the *case's* repository, not for this one. The Crisis Atlas
reference candidate ships `tests/test_services.py`, which imports
`crisis_atlas.services` -- a package that exists in the evaluation seed and
must never exist here.

Without this, pytest collects that file, fails to import it, and aborts the
whole run at collection. That failure is real information: it says evaluator
material was reachable from a context that had no business reaching it. The
answer is to make the boundary explicit rather than to rename the fixture into
something no longer representative of the task it encodes.

Note that this is a *collection* boundary only. It is not the containment
mechanism for the arms; that is
`ResolvedCasePackage.assert_arm_visible_set_is_contained`, which compares
resolved absolute paths against artifact kinds and does not trust any path
convention, including this one.
"""

from __future__ import annotations

collect_ignore_glob = [
    "docs/qualification/pilot/*",
    # Worktrees an arm actually produced, preserved as evidence. They are the
    # *output* of a run against the evaluation seed, so their tests import that
    # seed's packages -- `calc`, `crisis_atlas` -- which exist there and must
    # never exist here. Collecting them aborts the whole run at import time,
    # which reads like a broken suite and is really this same boundary being
    # crossed one directory further along.
    "docs/evaluation/*/produced-worktree/*",
    "docs/evaluation/*/*/produced-worktree/*",
]

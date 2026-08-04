# ADR 0102: Exclude repository metadata where the tree is walked

## Status

Accepted and implemented on 2026-08-03.

## Context

Three of the four completed Capability Sandbox tasks in `test project 6` list
`".git"` in `report.json`'s `files_changed`, and in
`context_attribution.changed_files` beside it. ADR 0063 exists precisely because
the reviewer-facing change surface must contain only model-authored work, and
`.git` is the harness's own metadata.

The cause was a narrow one. `workcell/delta.py` excluded `.git` as a *directory
name* while walking both trees. In the controller's disposable clone `.git` is
indeed a directory — but in the operator's managed Git worktree it is a
**file** containing `gitdir: ...`. The directory filter never saw the file, so
it was walked as ordinary content, differed between base and candidate, and
became a delta entry like any other. `checkpoint.py`'s `_paths_in` carried an
identical `item != ".git"` directory filter, and so shared the defect.

`tree_fingerprint` uses the same walk, so the candidate fingerprint that binds
admission was also computed over a file that is not the candidate's work.

## Decision

Repository metadata is excluded **by name, at the point the tree is walked, at
both the directory and the file level**. `EXCLUDED_DIRECTORY_NAMES` becomes
`EXCLUDED_METADATA_NAMES` (the old name is kept as an alias, since the rename is
itself the lesson: these were never only directories), and `checkpoint.py`
imports that same set instead of keeping its own literal. One source of truth
means the delta, the fingerprint, the readiness view and everything downstream
of them cannot drift apart again.

Exclusion is not silence. `CandidateDelta.excluded_metadata` records every
metadata path observed on either side, so "the candidate's `.git` differs from
the base's" stays in the audit trail; it is simply not model-authored work and
so does not reach a reviewer. An exclusion nobody can see is indistinguishable
from a walk that never encountered the path.

This is fixed at the collection point rather than by filtering the report.
`files_changed` and `context_attribution.changed_files` both derive from the
promoted delta, so one fix corrects both, and no later surface has to remember
to strip anything.

`.apoapsis` and `.sol` remain deliberately *not* excluded: they are controller
state, an agent writing into them is a boundary violation, and `classify_path`
marks them `FORBIDDEN` so admission refuses the candidate. Hiding them would
make the delta look clean for exactly the change admission exists to catch.

## Consequences

Reviewer-facing change surfaces contain only model-authored files. Candidate
fingerprints no longer include the base worktree's `.git` pointer, so they are
computed over the same content on both sides of the boundary — fingerprints from
before this change are not comparable with fingerprints after it, which is
correct rather than a compatibility problem, since each is bound to one run.
`__pycache__`, `.pytest_cache` and `node_modules` are now also excluded when
they appear as files rather than directories, which is the same rule applied
consistently.

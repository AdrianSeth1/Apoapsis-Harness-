# ADR 0099: Trust only the controller-owned copied base for Git cleanup

## Status

Accepted and implemented on 2026-08-01.

## Context

After ADR 0098 made the Windows task worktree readable to Linux, the product
controller reached `_base_tree` and copied the normalized seed into its
response runtime. The controller container runs as root while the bind-mounted
copy retains host ownership. Git correctly refused `git clean` with a dubious
ownership error before Qwen started.

## Decision

The controller passes `-c safe.directory=<exact copied-base-path>` to its two
Git cleanup commands (`clean` and `reset`). It does not write global or system
Git configuration and does not trust the source seed, mount root, repository
parent, or a wildcard. The target is the fresh controller-owned disposable
copy that is deleted/recreated for one run and stripped of `.git` immediately
after cleanup.

## Consequences

- UID differences at the Windows/WSL/container boundary no longer prevent the
  controller from sanitizing its own disposable base.
- Trust is command-scoped and path-exact.
- The source task worktree remains read-only and unmodified.
- Dirty-seed rejection remains in the launcher before the controller starts.

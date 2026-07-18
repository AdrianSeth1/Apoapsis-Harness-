# ADR 0007: Apoapsis product and runtime namespace

- Status: Accepted
- Date: 2026-07-17

## Context

The original short project name was too ambiguous for a coding harness. A new
name must identify the product consistently across user commands, Python
imports, persisted state, managed Git branches, environment variables, audit
language, and documentation. Keeping two active namespaces would perpetuate the
confusion and double the configuration and test surface before a public release.

## Decisions

1. The canonical product name is **Apoapsis Harness**.
2. The Python distribution is `apoapsis-harness`, the import package is
   `apoapsis`, and the command is `apoapsis`.
3. New project state is written only below `.apoapsis/`. The SQLite filename is
   `apoapsis.db`, and managed task branches use `apoapsis/<task>`.
4. Product-owned credential and live-test environment variables use the
   `APOAPSIS_` prefix. Third-party variables such as `OPENAI_API_KEY`,
   `GITHUB_TOKEN`, and Reddit credential names retain their provider-defined
   spelling.
5. This pre-release migration does not ship import, command, or state-directory
   aliases. Tests must import and exercise the canonical package and command.
6. Legacy `.sol/` directories remain ignored, protected from patches, excluded
   from context and research traversal, and excluded from cloud transmission.
   They are not active state and are never created by new runs.
7. Existing content-addressed evaluation audits and Git worktree pointers are
   immutable historical evidence. They are not rewritten or moved merely to
   change branding; their documentation labels the legacy path explicitly.

## Consequences

The runtime and user-facing namespace is unambiguous inside this repository.
Existing pre-release checkouts must initialize fresh `.apoapsis/` state. If an
old task audit must be retained, keep it read-only as historical evidence rather
than copying its database and rewriting artifact paths. A future supported data
migration would require its own schema, integrity validation, tests, and ADR.


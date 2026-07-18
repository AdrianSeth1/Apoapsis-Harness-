# ADR 0001: Deterministic substrate for the Apoapsis Harness MVP

- Status: Accepted
- Date: 2026-07-17

## Context

Apoapsis needs a trustworthy local control plane before it adds model-driven code
generation. The first milestone must preserve user constraints, isolate source
changes, persist every workflow decision, and let deterministic verification—not
a model—decide whether a patch passed.

## Decisions

1. **Python 3.12 is the first ecosystem.** The harness itself uses Python 3.12,
   Pydantic v2, SQLite, TOML, and the Git CLI. The CLI uses `argparse` to keep the
   first substrate dependency-light.
2. **The workflow is an explicit persisted state machine.** SQLite transitions
   are atomic and version-checked. Models may eventually return recommendations,
   but no model actor exists in the transition API.
3. **Hard constraints are immutable source evidence.** Their exact wording,
   interpretation, scope, source, status, verification method, and supersession
   are separate fields. Model requests fail validation unless every active
   constraint has a coverage disposition.
4. **Context is content-addressed evidence.** File excerpts carry line ranges,
   commit provenance, inclusion reasons, SHA-256 digests, and transmission policy.
5. **Unified diff is the initial patch wire format.** Structured edits remain in
   the schema for later controlled adapters. This milestone applies neither.
6. **Verification commands are argument vectors, never shell strings.** They run
   sequentially with timeouts, an environment allowlist, bounded captured output,
   and persisted structured results. A host process runner is the portable MVP;
   container enforcement is a later execution adapter.
7. **Git worktrees provide source isolation, not security isolation.** Each task
   receives a dedicated `apoapsis/<task>` branch under a controlled directory.
   Package installation, network use, secrets, and destructive operations will
   require separate policy enforcement before model execution is enabled.
8. **The future local adapter defaults to an OpenAI-compatible endpoint.** This
   supports servers such as Ollama without hard-coding a model. OpenAI is the
   first planned frontier adapter. Neither is implemented in this milestone.
9. **Routing starts with rules.** Security/authentication, concurrency,
   migrations, multi-service changes, destructive actions, and sensitive-data
   work are prohibited from local-only execution. There is no learned router.
10. **Memory is versioned against repository truth.** Future project constraints
    use project scope and task constraints use task scope. Evidence and memory
    record their commit; a HEAD change invalidates repository-wide claims, while
    affected-path or affected-symbol changes invalidate scoped claims.

## Context and relevance policy for later milestones

Context budgets reserve full space for task text and active constraints before
allocating the remainder to evidence. Retrieval starts with named paths, errors,
definitions, references, imports, tests, and recent history. Lexical and semantic
ranking follow. Test relevance uses references/imports and co-change history when
names do not match. Exact percentages remain configurable because repository
languages and model limits vary.

## Consequences

The MVP is auditable, replayable, and testable without any model credentials. It
does not yet provide a security sandbox, natural-language constraint extraction,
patch generation/application, semantic repository analysis, routing, memory, or
provider calls. Those capabilities must build on these persisted schemas and
cannot bypass their fail-closed checks.


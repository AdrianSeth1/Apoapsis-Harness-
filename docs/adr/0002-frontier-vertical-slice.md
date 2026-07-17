# ADR 0002: One bounded frontier-model vertical slice

- Status: Accepted
- Date: 2026-07-17

## Context

The `substrate-v0.1` baseline provides schemas, persisted workflow transitions,
worktrees, and deterministic verification. The next useful increment must prove
an end-to-end frontier flow without granting a model workflow or tool authority.

## Decisions

1. A provider receives one prompt and returns one untrusted proposal. An
   instrumentation wrapper—not the adapter—owns latency, cache, token, and cost
   accounting.
2. The first adapter targets an OpenAI-compatible chat-completions endpoint. URL,
   model, credential environment variable, timeout, and pricing are project
   configuration.
3. Specification extraction is model-assisted but Pydantic-validated. Every hard
   constraint must retain a case-sensitive substring from the original request.
4. Context selection is deterministic and Python-first: Git inventory/current
   diff, explicit paths, ripgrep, Python AST symbols/imports, and related tests.
   Every excerpt carries path, lines, commit, reason, digest, and transmission
   policy.
5. The exact context package and prompt are atomically written before a provider
   call. Provider responses, telemetry, patches, policy findings, verification,
   failures, and the final report are separate audit artifacts.
6. Provider patch output must be only a Git unified diff. Deterministic policy
   rejects path escapes, dependency or verification changes, deleted tests,
   binary changes, and configured file/line limits before `git apply --check`.
7. Verification may trigger exactly one focused frontier repair. The repair sees
   the approved task, constraints, current diff, failing command, normalized root
   failure, and focused source/test evidence. A second failure ends the task.
8. Only the verification runner can produce a successful outcome. Model finish
   reasons and prose never determine workflow state.

## Consequences

The milestone supports one frontier provider and a reproducible, inspectable
patch loop. It deliberately excludes local coding models, embeddings, learned
routing, autonomous agents, web UI, and broad work automation. The host verifier
still is not a container sandbox; users must configure commands they trust.


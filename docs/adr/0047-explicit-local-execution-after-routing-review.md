# ADR 0047: Explicit local execution after routing review

## Status

Accepted in the working tree on 2026-07-21. Deterministic coverage was added but
not run at the owner's request.

## Context

Slice 5 (`TASK-AAAC2F3DA4B0FD8EDF8635D1`) appeared to have exhausted its agent
turns and asked for human intervention. Its durable evidence showed a different
failure: `agent_turns`, model calls, patches, and verification runs were all zero,
and no worktree or agent-session artifact existed. AUTO routing classified the
slice as high risk, selected a frontier path, found no configured frontier coder,
and emitted `deterministic_route_requires_human` before implementation began.

The existing local continuation action could not be used safely because there
was no session to continue. Treating a fresh run as a continuation would give the
wrong audit history and budget semantics. Permanently changing AUTO routing would
also weaken the deterministic default for every future task rather than capture
the user's decision for this one operation.

## Decision

Add `ReviewActionKind.AUTHORIZE_LOCAL_STAGE`, displayed as **Run locally**, only
for `ROUTING_REQUIRES_HUMAN`. It is a fresh execution action, not a continuation.
Eligibility remains harness-computed and execution rejects the action if a task
worktree or local agent session already exists.

Explicit confirmation transitions the task back to `SPEC_APPROVED`, records a
user-authored `review_local_stage_authorized` event, and starts a normal durable
execution operation with an operation-scoped `local_only` route override. The
stored project configuration is not modified. The child operation still owns
execution authorization, provider construction, isolated worktree creation,
bounded model interaction, patch validation, configured verification, completion
decisions, reports, and audit artifacts.

If startup fails while the task is still `SPEC_APPROVED`, the harness returns it
to Human Review with `review_local_stage_start_failed`. That event classifies as
the same routing-review reason so inspection and an explicit retry remain
available instead of falling into an unknown review state.

The CLI equivalent is `apoapsis review run-local`; the routine product path is
the UI button and requires no terminal use.

Completing the background review operation is not itself task success. If the
fresh local execution returns to Human Review, the UI labels it **Local run
incomplete** and states that required verification and dependent slices remain
blocked.

## Consequences

A task stopped before implementation can be deliberately run with the configured
local coder without pretending an earlier session exists or weakening routing
defaults globally. The user's authorization and the one-operation override are
durable and auditable. Models receive no new shell, filesystem, Git, network,
workflow, verification, completion, retry-limit, or audit authority.

Deterministic coverage exercises routing-review eligibility, successful fake
local execution through verification, startup-failure classification, CLI
parsing, and the UI's truthful **Run locally** copy. The owner can verify with:

```powershell
python -m unittest tests.test_review tests.test_review_execution tests.test_cli tests.test_ui_copy_and_accessibility -v
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

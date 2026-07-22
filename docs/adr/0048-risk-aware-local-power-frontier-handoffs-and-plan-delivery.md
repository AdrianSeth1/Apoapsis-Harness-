# ADR 0048: Risk-aware local power, complete frontier handoffs, and plan delivery

## Status

Accepted in the working tree on 2026-07-21. Deterministic coverage was added but
not run at the owner's request.

## Context

Three workflow gaps remained after Slice 5's routing diagnosis:

1. AUTO high-risk routing skipped local execution and either called frontier
   directly or stopped before any agent when frontier was absent. This wasted the
   configured local coder's capability and made local execution require recovery.
2. Automated frontier escalation carried compiled context and local attempt
   evidence, but the manual ChatGPT/Claude coding package mostly carried the
   specification, diff, and failures while claiming to be self-contained.
3. Completing the final plan slice left the integrated application on an internal
   task branch. There was no product action to obtain the finished project or ask
   a frontier model to review all slices together.

## Decision

### High-risk local profile

AUTO routes HIGH work `LOCAL_THEN_FRONTIER` when frontier exists and `LOCAL_ONLY`
otherwise. The effective execution configuration uses the maximum finite local
loop/search/read ceilings supported by `AgentLoopConfig`. Repository context uses
the maximum compiler breadth while its character budget remains tied to the
configured local model's declared context window. The immutable execution
authorization package is built from this effective configuration, so the larger
budget is visible and hash-bound before execution. The same effective local
profile is re-derived for an explicitly authorized continuation, so repair does
not silently fall back to the ordinary task budget.

CRITICAL work still stops for explicit routing review. A pre-agent routing review
offers **Run locally** and, when configured, **Run with frontier**. These are fresh
durable execution operations with one-operation route overrides. They are not
continuations, do not change project defaults, and do not grant model authority.

### Frontier handoff completeness

Manual frontier coding package schema 1.1 adds:

- deterministic cloud-safe repository context;
- every persisted local/frontier agent session;
- complete verification results as well as normalized failures; and
- the exact hash-matching approved plan-slice package when the task came from a
  plan.

CLI and UI exports call one shared compiler. Existing 1.0 package integrity
verification excludes the new defaulted fields so old immutable artifacts remain
readable. Automated escalation continues to use its existing context compiler,
current diff, normalized failures, and complete local action history; the stronger
high-risk context profile also applies there.

### Finished-plan delivery

When every slice's authoritative task state is COMPLETE, the Plan UI offers
**Prepare finished project**. The harness checkpoints all completed task branches,
requires one descendant commit containing them all, and fails closed on divergence.
It creates a Git archive from that exact commit, adds a plain usage guide, writes
hash-bound delivery metadata and a whole-project frontier-review handoff, then
records the plan as EXECUTED. The ZIP excludes `.git`, Apoapsis runtime state,
ignored secrets, and credentials by construction.

The review handoff instructs a frontier model to inspect the ZIP as one integrated
application and return structured architecture, integration, security,
operability, release-readiness, and verification-gap findings. Apoapsis does not
claim the external review ran, apply its findings automatically, deploy the app,
or infer project-specific credentials/hosting.

## Consequences

High-risk tasks get the local model's best bounded chance before paid or manual
frontier help. Frontier models receive the evidence needed to reason about the
real task rather than a diff in isolation. A completed plan has a visible,
downloadable outcome and optional whole-code review path without touching the
operator's checked-out branch.

Models remain untrusted proposers. Apoapsis still owns context selection, provider
construction, filesystem/Git actions, dependency installation, patch validation,
verification, workflow transitions, completion, delivery, and audit history.

The owner can verify with:

```powershell
python -m unittest tests.test_agent_loop tests.test_review tests.test_review_execution tests.test_manual_frontier tests.test_manual_frontier_ui tests.test_architect_slice tests.test_architect_slice_ui tests.test_ui_copy_and_accessibility -v
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

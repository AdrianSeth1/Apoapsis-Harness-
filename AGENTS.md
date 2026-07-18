# Instructions for coding models

Read `HANDOFF.md` before making changes. It is Apoapsis Harness's canonical living
architecture and project-status handoff.

Then read `NEXT_STEPS.md` for the current owner/coding-agent priority order. For
application design or implementation, also read `docs/product-design-handoff.md`;
the design brief does not itself authorize a UI architecture or weaken the
authority boundary in `HANDOFF.md`.

For every change that affects architecture, workflow behavior, configuration,
model roles, context, patch policy, verification, audit artifacts, tests, or
evaluation evidence:

1. Update `HANDOFF.md` in the same change using its Documentation update
   triggers and maintenance checklist.
2. Update `README.md` for user-visible behavior.
3. Add a new ADR for a new architectural decision; preserve existing ADRs as
   decision history.
4. Add deterministic fake-provider coverage for model-driven workflow branches.
5. Run focused tests, the full test suite, `python -m compileall -q src tests`,
   and `git diff --check`.
6. Refresh the handoff Snapshot only with results actually observed. Distinguish
   fake-provider integration, live local inference, and live hosted inference.
7. Preserve uncommitted user work and the `substrate-v0.1` tag. Never reset or
   discard changes merely to obtain a clean tree.

Models are untrusted proposers. Do not grant a model direct shell, filesystem,
Git, network, workflow-transition, retry-limit, verification, completion, or
audit authority. If a proposed change alters that boundary, stop and require an
explicit architectural decision.

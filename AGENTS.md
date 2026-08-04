# Instructions for coding models

Read `HANDOFF.md` before making changes. It is Apoapsis Harness's canonical living
architecture and project-status handoff, and it is deliberately short: it
describes the system as it is now.

Do **not** read `docs/history/handoff-archive-2026-08.md` by default. It holds
the full narrative behind each Snapshot row and each ADR — moved out on
2026-08-03 so that reading the handoff stops costing tens of thousands of tokens
per session. Consult it only when your task actually touches that history, and
then only the rows concerned.

Then read `NEXT_STEPS.md` for the current owner/coding-agent priority order. For
application design or implementation, also read `docs/product-design-handoff.md`;
the design brief does not itself authorize a UI architecture or weaken the
authority boundary in `HANDOFF.md`.

For every change that affects architecture, workflow behavior, configuration,
model roles, context, patch policy, verification, audit artifacts, tests, or
evaluation evidence:

1. Update `HANDOFF.md` in the same change using its Documentation update
   triggers and maintenance checklist. Respect its section rule: current state
   in `HANDOFF.md`, dated narrative in the archive or an ADR.
2. Update `README.md` for user-visible behavior.
3. Add a new ADR for a new architectural decision; preserve existing ADRs as
   decision history.
4. Add deterministic fake-provider coverage for model-driven workflow branches.
5. Run focused tests, the full test suite, `python -m compileall -q src tests`,
   and `git diff --check`. The full suite exits 0 as of ADR 0111 and there is
   no known-failure inventory to excuse a red run. If you make it red, fix it
   or skip the test with a reason that names the missing capability and what
   would unblock it — never a bare "not supported here", and never by deleting
   an assertion.
6. Refresh the handoff Snapshot only with results actually observed. Distinguish
   fake-provider integration, live local inference, and live hosted inference.
7. Preserve uncommitted user work and the `substrate-v0.1` tag. Never reset or
   discard changes merely to obtain a clean tree.

Models are untrusted proposers. Do not grant a model direct shell, filesystem,
Git, network, workflow-transition, retry-limit, verification, completion, or
audit authority. If a proposed change alters that boundary, stop and require an
explicit architectural decision.

# ADR 0040: Required verification scaffolding is implementation work

- Status: Accepted
- Date: 2026-07-21

## Context

A continued blank-repository task successfully applied its Gmail implementation,
then ran the required initialized unittest command. Discovery failed because the
`tests/` directory did not exist. Although `allow_test_changes` was true and the
prompt said tests *may* be added, the local model requested escalation because the
approved product objective did not separately require test files.

This is mechanically repairable repository work, not an external-authority or
capability blocker. Treating tests as optional makes an initialized required check
unreliable and wastes continuation turns.

## Decision

The harness now derives live verification-scaffolding obligations from required
commands and the current task worktree. When required Python unittest discovery
targets a missing directory and test edits are allowed, every agent turn explicitly
states that the model must create the importable directory and meaningful,
task-focused tests before verification. The task specification need not separately
ask for test files: satisfying configured required verification is part of the
implementation contract.

If the latest verification output confirms the known missing-discovery-directory
failure, a model request to escalate is rejected as non-actionable. The rejected
turn is audited, the repair instruction enters session history, and the bounded
agent continues within its existing turn and patch budgets. Other escalation
reasons remain unchanged.

Prompt policy also requires third-party imports to be declared in a recognized
manifest. ADR 0041 authorizes harness-controlled installation before verification;
tests should still mock credential, browser, and remote-service boundaries unless
the approved task and configured checks explicitly require live integration.

The harness does not invent application tests, execute raw model-supplied commands,
waive verification, or mark completion. The untrusted model still proposes the test
and manifest patches; normal parser, path policy, patch ceilings, Git apply, the
harness-selected ADR 0041 installer, and verification controls remain authoritative.

## Verification

Deterministic fake-provider coverage removes a fixture's test directory, applies an
implementation, observes the real unittest discovery failure, attempts escalation,
then verifies that escalation is rejected and a subsequent task-focused test patch
can proceed to verification. Per the owner's request, no tests, compile check, or
diff check were run for this change.

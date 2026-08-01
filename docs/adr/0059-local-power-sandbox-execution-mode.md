# ADR 0059: Local Power Sandbox execution mode (experimental)

- Status: Accepted (experimental, disabled by default)
- Date: 2026-07-25

## Context

ADRs 0057 and 0058 both address the same underlying observation from different
angles: Poolside Laguna S 2.1, served locally, produces plausible *code* and
implausible *protocol*. The recorded failures are not reasoning failures. They
are malformed unified-diff hunks, missing `+` markers, a second `diff --git`
header opened inside a live hunk, llama.cpp tool-call residue copied into file
content, cross-action fields such as `command_name` attached to a `create_file`,
and long retry cycles spent re-authoring diff syntax that was never going to
parse.

Each of those got a targeted fix. `create_file` (ADR 0057) removed the need to
hand-author a new-file diff at all; the tool-noise normalizer (ADR 0058)
recovered one specific template artifact. Both helped. Neither addressed the
general shape of the problem, which is that the strict loop asks a small local
model to be simultaneously correct about *what to write* and *how to encode the
write*, and it is reliably only correct about the first.

That raises a question the harness cannot answer by reasoning about it: is
Laguna's poor showing a capability limit, or a protocol tax? If it is a
protocol tax, then removing diff authorship should move the numbers. If it is a
capability limit, removing diff authorship will change nothing and this mode
should be deleted rather than developed further.

## Decision

Add an opt-in, clearly experimental second execution path: the Local Power
Sandbox. It is a *separate session class*, not a widening of
`BoundedAgentSession`.

The model gets a looser protocol inside a disposable sandbox:

- `read_file`, `search`, `write_file`, `delete_file`, `run_shell`,
  `run_verification`, `finish`
- `write_file` takes **whole-file content**, never a diff. The harness computes
  the diff afterwards from the sandbox's Git state.

The model does **not** get any new authority. Specifically it still cannot:

- reach the Apoapsis source repository, `.apoapsis/**`, `.git/**`, `.env*`,
  key/certificate material, the user's home directory, or any absolute or
  `..`-traversed path;
- run anything but allowlisted programs, and never through a shell;
- see credentials in the command environment;
- reach the network (denied by default);
- mutate workflow state or the audit log;
- decide that the task is complete.

Enforcement lives in two harness-only modules:

- `apoapsis.agent.sandbox` — `SandboxGuard` (lexical normalization, forbidden
  globs applied to a path *and every ancestor*, real-path containment that
  catches symlinked parents, binary and size limits) and `SandboxShell`
  (metacharacter refusal, prefix allowlist, path-argument containment, scrubbed
  environment, hard timeout, capped output).
- `apoapsis.agent.power_session` — `LocalPowerSession`, which mediates every
  action, restores the sandbox byte-for-byte when a change-budget ceiling is
  crossed, computes the final diff, runs configured verification, and assembles
  a `LocalPowerReviewPackage`.

### Completion authority is unchanged

`finish` ends the model's turns and nothing else. After the loop stops — by
`finish`, by turn exhaustion, or by wall-clock exhaustion — the harness runs the
configured verification and decides. A session with no changes, with no
configured verification commands, or with failing verification reports
`ESCALATION_REQUIRED` and produces a human-review package. Under the strict
completion policy, acceptance coverage must additionally be proven, exactly as
in the strict loop.

The model's `finish` summary is carried into the review package as
`model_summary` and labelled in the UI as a claim, not a result.

## Configuration

```toml
[execution.local_power]
enabled = false            # opt-in; the strict loop remains the default
workspace = "isolated_worktree"
allow_shell = true
allow_network = false
max_turns = 8
max_seconds = 1800
max_shell_commands = 40
max_changed_files = 100
max_changed_lines = 10000
require_final_diff_review = true
require_verification = true
```

`forbidden_paths` may be widened by a local override but a validator refuses any
list that drops `.apoapsis/**`, `.git/**`, `.env`, or `.env.*`. Enabling the
mode requires `execution.mode = "agent"` and is incompatible with the
`frontier_only` route — this is a local-model experiment and never the frontier
coder's path.

## Consequences

The strict one-action loop is untouched and remains the documented default;
existing measurements stay comparable. The experiment is cheap to evaluate and
cheap to remove: deleting `power_actions.py`, `power_session.py`, `sandbox.py`,
one config block, and one branch in `_run_bounded_agent` reverts it completely.

The honest risk is that whole-file writes make a *large wrong change* as easy to
produce as a small right one — a 500-line file rewritten to fix one line is a
worse review artifact than a targeted diff, even though it is equally contained.
The change-size budgets bound the blast radius but do not make the diff
reviewable. If this mode graduates from experimental, that is the problem to
solve next.

`require_final_diff_review` currently guarantees the review package is always
produced; it does not yet gate an apply step, because there is no automatic
apply for this path. Acceptance remains a harness/user action.

## Verification

`tests/test_local_power_session.py` covers the boundary deterministically with a
scripted fake provider: opt-in defaults, in-sandbox writes, refusal of
`.apoapsis/config.toml`, `.git/config`, `..` traversal, absolute paths, and
forbidden reads; sandbox cwd, scrubbed environment, enforced timeout, and
audited refusal for shell; harness-computed diff; verification-determined
outcome; `finish` not implying completion; failed verification producing human
review; passed verification producing a normal report package; and a review
package containing the transcript, writes, commands, rejections, and
verification results.

Run with `python -m unittest tests.test_local_power_session -v`.

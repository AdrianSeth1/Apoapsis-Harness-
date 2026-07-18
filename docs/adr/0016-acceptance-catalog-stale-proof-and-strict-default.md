# ADR 0016: Acceptance-command catalog, stale-proof correction, and STRICT as the product default

- Status: Accepted
- Date: 2026-07-18

## Context

A review of ADR 0015 (verification layers and acceptance coverage) found
three defects before any live strict evaluation should run:

1. Specification extraction had no visibility into which verification
   commands actually exist or which are acceptance-designated. A model
   proposing `AcceptanceCriterion.verification_method` was guessing at a
   free-text name with no closed vocabulary, and nothing validated the
   guess against real configuration before it entered an approved
   specification.
2. `compute_acceptance_coverage()` took a flat `passed_command_names: set[str]`.
   Anything not in that set -- whether it had genuinely failed or had
   simply **never been run** -- was classified `FAILED`. That conflates two
   different evidentiary states and is not what "Failed" should mean.
   Separately, nothing in the data passed to the function encoded *which
   worktree digest* a pass was recorded against, so a design that
   accumulated "ever passed" history across edits could let an earlier,
   now-superseded result silently prove present-day code.
3. `CompletionPolicy.BASELINE` was every project's practical default
   (`apoapsis init` wrote it explicitly), meaning ordinary product runs
   never got the correctness guarantee ADR 0015 built, while nothing forced
   evaluation runs measuring false success to make an explicit, audited
   policy choice independent of whatever a real project happened to select.

This ADR corrects all three without rewriting ADR 0015 -- that document
remains accurate design history for the three-layer model, the coverage
schema shape, and the `BASELINE`/`STRICT` split; only the inputs to
coverage computation, the extraction contract, and the practical default
change here.

## 1. Deterministic acceptance-command catalog at extraction time

`SpecificationExtractor.build_prompt()` and `.parse()` both gain an
`acceptance_catalog: Sequence[VerificationCommand]` parameter. The prompt
now includes an `ACCEPTANCE_COMMAND_CATALOG` block: a sorted JSON array of
`{name, category, description, acceptance_designated}` built fresh from the
real `[verification.commands]` configuration on every call -- never
hand-edited, never stale. `VerificationCommand` gains a plain descriptive
`description: str` field (default `""`) purely for this catalog; it is
never executed and grants no capability.

The prompt instructs the model that `verification_method` may be set only
to a catalog `name`, or left `null`; it must never invent a command or
propose a shell string. `parse()` enforces this: any
`acceptance_criteria[*].verification_method` naming something absent from
the catalog raises `SpecificationExtractionError`, the same fail-closed
pattern already used for verbatim hard-constraint violations. This is
strictly a closed-vocabulary check, not new authority -- the model still
cannot mark a command `acceptance = true`, execute one, or grant itself
proof; it can only point at a name the harness already knows about, and the
user still approves the whole specification, mapping included, before any
of it takes effect. The existing UI specification view now renders each
criterion's proposed mapping (`PROPOSED CHECK: <name>` or "none") so the
approval the user gives is genuinely informed by what will be asked to
prove that criterion.

## 2. Coverage computation consumes explicit per-command execution states

`compute_acceptance_coverage()`'s third parameter changes from
`passed_command_names: set[str]` to `command_results: dict[str,
VerificationStatus]`, mapping a command name to its most recent
`VerificationStatus` **restricted to the current worktree digest only**.
`SKIPPED` entries must be omitted by the caller (a skipped command was
never actually executed). The resulting semantics:

- name absent from the mapping → **Unproven** ("has not yet been executed
  for the current worktree state").
- name present with `FAILED`/`TIMED_OUT`/`ERROR` → **Failed** ("did not
  pass ... for the current worktree state").
- name present with `PASSED` → **Proven** ("passed ... for the current
  worktree state").

`BoundedAgentSession` replaces its old `passed_checks: dict[str, set[str]]`
with `command_results: dict[str, dict[str, VerificationStatus]]`, keyed by
the same worktree-diff digest (`_verification_state_digest()`) already
used to de-duplicate identical verification runs. Every `_verify()` call
records each executed command's real status under the *current* digest's
entry; a lookup always asks for the current digest's entry specifically, so
a status recorded for an earlier digest is simply never visible once the
worktree changes -- there is no cross-digest merge, accumulation, or
fallback. `workflow/vertical_slice.py`'s one-shot path builds the
equivalent mapping directly from the just-completed `VerificationResult`
each time, which is inherently single-digest. `tests/test_acceptance_
coverage.py` proves this directly: a criterion's mapped command passes,
the worktree is then edited (a new digest, no functional regression), and
that criterion reverts to Unproven until the model re-runs the same check
against the new code -- only then does coverage (and completion) restore.
A companion unit-test class exercises the never-executed/failed/
timed-out/error/passed/skipped states directly against
`compute_acceptance_coverage()` without the full harness.

## 3. STRICT becomes the practical product default; evaluation stays explicit BASELINE

The `CompletionPolicy` Pydantic field default is left at `BASELINE` --
changing it would silently flip every existing hand-constructed
`ApoapsisConfig` in the test suite and any embedding caller that never
reads `apoapsis init`'s template, which is a much larger and less
deliberate blast radius than this correction intends. Instead, **`apoapsis
init`'s generated `.apoapsis/config.toml`** -- the config every ordinary
product run actually gets -- now writes `completion_policy = "strict"`
explicitly, and marks its one default verification command `acceptance =
true` so the default configuration is immediately usable (an extracted
specification whose criteria map to that command can actually reach
`COMPLETE`, not merely fail closed on every task from turn one). This is
the practical, testable meaning of "STRICT is the default for ordinary
product runs": a fresh `apoapsis init` opts a project in, and doing so
requires nothing else the user must discover.

`apoapsis eval`'s lane overlay (`evaluation/lanes.py`) now forces every
lane's `execution.completion_policy` to `CompletionPolicy.BASELINE`
explicitly, regardless of what the caller's real project configuration
selects -- a real project may now default to `STRICT`, and without this
override every evaluation run would silently inherit it, changing what
"false success" measures out from under anyone comparing runs over time.
This is a deliberate, audited override (a code comment names the reason),
not accidental inheritance, and it is recorded on every persisted
`FinalTaskReport.completion_policy` plus a new "Completion Policy" column
in `apoapsis eval`'s comparison Markdown -- the selection is visible per
lane, not buried.

## Authority and safety (unchanged)

Nothing here grants a model new authority. The catalog is a read-only,
harness-built list of names; the model still only proposes a mapping,
never approves one, never executes an arbitrary command, and never asserts
a status. The bounded inspect/edit/test/diagnose/repair action set is
unchanged -- no new agent action was added, and the existing full
regression suite (197 tests before this change) passes unmodified, proving
the tool-freedom and iteration model these corrections sit on top of was
not touched. The held-out evaluation oracle (ADR 0012) remains untouched
and unimported by `workflow/`/`agent/`; this ADR does not reuse it as the
model-visible acceptance check, and does not add one to the
download-service fixture -- that mapping is future evaluation work, not
part of this correction.

## Non-goals

- Not a live strict-policy evaluation. This ADR makes the mapping path
  usable and the default safe; running a real local/frontier model against
  it, with a real acceptance mapping on a real fixture, is separate,
  future work (`NEXT_STEPS.md`).
- Not a change to the download-service fixture's specification or a new
  acceptance-designated command for it.
- Not a change to retrieval, context compilation, or the 64k default.
- Not a new agent action, workflow state, or transition.

## Consequences

A model can now only propose an acceptance mapping from a real,
harness-published catalog, and that proposal is validated before it can
enter an approved specification. Coverage is computed from genuine,
digest-scoped execution evidence, so a criterion's proof cannot outlive the
code it was proven against. Ordinary `apoapsis init` projects now default
to the correctness-first policy, while `apoapsis eval` explicitly and
visibly opts back into the historical baseline it needs for comparable
false-success measurement. The full pre-existing test suite is unaffected,
confirming these are corrections to a new mechanism, not changes to the
harness's core authority boundary or agent workflow.

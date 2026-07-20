# ADR 0029: D4c diagnostic probe infrastructure

- Status: Accepted
- Date: 2026-07-19

## Context

Commit D4b (2026-07-20, see `docs/evaluation/apoapsis-planning-comparison-
2026-07-20.md`) ran three monolithic and three planned live attempts
against `qwen3-coder-next:q4_K_M` on the `download-service-v2` fixture
(ADR 0028). All six stopped at `HUMAN_REVIEW_REQUIRED` after exhausting
their turn budget, having called `run_check`/`submit_for_verification`
zero times.

A read-only forensic pass over the preserved turn/call artifacts (this
commit) found the exact shape of the failure: every session made one
accepted edit, re-read the just-edited file once (legitimate -- new
content), and then, from that point to the end of its budget, re-issued a
**byte-for-byte identical** `read_file` request -- the raw model output
across consecutive turns is character-for-character the same JSON. This
is not a harness defect: turn/patch/verification accounting, escalation
classification, and dependency gating all behaved correctly, and the
current diff plus the configured verification-command names were clearly
present in every turn's prompt. Two independent, real but non-causal
findings surfaced along the way and are deliberately **not** fixed here
(see "Deferred follow-ups" below): `search_repository` fails with a raw
`[WinError 2]` on this machine (no fallback when `rg` cannot be resolved
by `CreateProcess`, unlike the context compiler's own lexical fallback),
and a turn's `REPOSITORY_EVIDENCE` can show a stale pre-edit copy of a
file alongside the fresh post-edit copy with no staleness label.

Critically, this loop is **not general model behavior**: every other
preserved live Qwen3-Coder-Next Q4 session under `.apoapsis-eval/`
(`local-strict-{1,3,4,5,6}`, `smoke-local`, `priority-a-64k`/`-run2`/
`-run3`, `priority-a-128k-run2`/`-run3` -- ten sessions, same model
digest, same `temperature=0.0`/`think=false`) reliably transitions from an
edit to `inspect_diff`/`run_check` within one or two turns, and even the
sessions that still exhaust budget without completing spend their
remaining turns cycling through `run_check`/`inspect_diff`/`replace_text`
-- never stuck reissuing one identical action for five or more consecutive
turns. The loop is 6/6 reproducible only on `download-service-v2`, in
both conditions, and nowhere else in the preserved evidence. This rules
out "the model never calls verification" as an explanation and points at
something specific to this fixture/task/prompt combination that has not
yet been isolated.

Per instruction, D5 and another full 3+3 planning comparison are both out
of scope until at least one probe demonstrates a model invoking real
verification. This ADR covers only the diagnostic infrastructure needed
to run controlled, single-variable probes -- it authorizes no live
execution itself.

## Decision

### Scope: evaluation-only, not a production prompt or workflow change

Every new capability in this milestone lives in `apoapsis.evaluation.
diagnostic_probe`/`diagnostic_probe_report` and a new `apoapsis eval-
planning-probe` CLI command. The only changes outside `evaluation/` are
two additive, default-safe constructor parameters:

- `BoundedAgentSession.__init__`/`.resume()` (`agent/session.py`) gained
  `agent_step_prompt_fn: AgentStepPromptBuilder = agent_step_prompt` --
  defaulting to the exact, unmodified production function. `run()` now
  calls `self.agent_step_prompt_fn(...)` instead of the module-level
  function directly, but no product call site (`vertical_slice.py`,
  `review/execution.py`) passes anything, so every existing call
  continues to construct the identical, byte-for-byte production prompt.
  `_AGENT_STEP_STATIC_PREFIX` itself was not touched.
- `VerticalSliceRunner.__init__` gained `agent_step_prompt_fn:
  AgentStepPromptBuilder | None = None`. `_run_agent_session` only adds
  the `agent_step_prompt_fn` keyword to its `BoundedAgentSession(...)`
  construction when the runner's own value is not `None` -- when omitted
  (every product CLI/UI/service call site, and `run_monolithic_condition`/
  `run_planned_condition`), `BoundedAgentSession` falls back to its own
  default, so the parameter is never even passed in the ordinary case.

Both defaults were chosen so that "not passing the new parameter" and
"today's behavior" are the same code path, not merely equivalent output --
provable by construction, and directly tested (see "Tests" below).

### The evaluation-only prompt variant (Probe 2)

`diagnostic_probe.progress_advisory_agent_step_prompt` has the identical
signature as `models.prompts.agent_step_prompt`. It calls the production
function first, unmodified, and appends exactly one short section:

```
PROGRESS_ADVISORY_NOTE (evaluation-only diagnostic addition, ADR 0029; not part of the production prompt)
- Repeating a read of an unchanged file range that already added no new
  evidence does not advance the task.
- After an accepted edit, inspect the current diff and run the
  appropriate configured verification command.
- If no useful action remains, request escalation instead of repeating a
  no-progress action.
This note is advisory only. It does not select, force, or forbid any
specific action; Apoapsis alone still decides whether a requested action is
executed.
```

It never edits, reorders, truncates, or removes anything the production
prompt already contains, never adds a new allowed action, and never
changes the action JSON schema (`agent/actions.py` untouched). The
harness still validates, executes, or rejects whatever action a model
actually requests exactly as before -- this note cannot cause a specific
action to be chosen, only (if the model attends to it at all) discourage
one already-diagnosed non-progress pattern.

### The alternate-model probe (Probe 3): fail closed

`diagnostic_probe.AlternateModelSpec` names a substitute model;
`alternate_model_provider_config()` clones the project's own configured
`local_coder`/`frontier` `FrontierProviderConfig` with only `.model`
overridden -- every decoding setting (temperature, context window,
think, timeout, base URL) is inherited unchanged, so the model identity
is the single intended independent variable.

Three independent, mandatory conditions are enforced before any provider
is constructed or any model is called; any one failing alone raises
`DiagnosticProbeError` and stops:

1. **The alternate model must actually differ from the project's
   configured coding model.** The CLI compares `--alternate-model`
   against `config.models.local_coder`/`frontier`'s current `.model`
   right after loading configuration and rejects an exact match --
   requesting "a different model" that resolves to the same model would
   silently vary zero independent variables while claiming to vary one.
2. **Explicit authorization**: the model name must appear in an
   `authorized_model_names` set the caller supplied explicitly -- never a
   default, never inferred. The CLI requires a second, separate
   `--authorize-alternate-model` flag that must exactly match
   `--alternate-model`; a mismatch is rejected before any filesystem or
   config access at all (`verify_alternate_model_authorized()`).
3. **Actually installed**: the model must actually be installed at the
   configured Ollama endpoint right now, checked via one read-only `GET
   /api/tags` (`InstalledModelLister`, injectable for deterministic
   tests; the default implementation never starts, stops, downloads, or
   configures anything) -- also inside `verify_alternate_model_
   authorized()`, checked only after authorization already passed.

No condition is checked against another's result set -- an
authorized-but-uninstalled name never falls back to a default model, and
an installed-but-unauthorized name is never silently substituted in.

### Enforcing exactly one independent variable, fail closed

The single most important invariant a probe must never violate: it may
vary the prompt condition *or* the model identity, never both, and never
neither in a way that silently changes nothing. `diagnostic_probe
.validate_single_independent_variable(prompt_condition, model_selection)`
is the one authoritative, pure (no I/O) check of this invariant --
`PROGRESS_ADVISORY` may only run against `model_selection.source ==
"project_local_coder"`; `model_selection.source == "explicit_alternate"`
may only run under `PromptCondition.PRODUCTION`. It is called from two
independent places, deliberately redundant rather than trusting either
caller alone:

- `run_single_slice_diagnostic_probe()` calls it as its literal first
  statement, before `package_slice`/`approve_slice` or anything else --
  so even a caller that reaches the orchestration function directly,
  bypassing the CLI entirely (as several of this milestone's own tests
  do), cannot violate the invariant.
- The CLI (`_eval_planning_probe`) additionally checks the raw
  `--alternate-model`/`--prompt-condition` argument combination *before
  any filesystem access at all* (a `--alternate-model` value implies
  `--prompt-condition production`), and again calls the shared validator
  once `model_selection` is actually constructed, as defense in depth
  against the two checks ever drifting out of sync.

Combined with the same-model-rejection check above, a probe request can
now only ever land in one of exactly three valid states: `PRODUCTION`
against the project's own model (Probe 2's baseline / D4b's own
condition), `PROGRESS_ADVISORY` against the project's own model (Probe
2), or `PRODUCTION` against a genuinely different, authorized, installed
alternate model (Probe 3). Every other combination is rejected before any
filesystem access, installed-model lookup, or provider construction.

### Orchestration: one slice, direct `VerticalSliceRunner`, not `start_slice`

`run_single_slice_diagnostic_probe()` calls the exact, unmodified
`architect.slice_service.package_slice`/`approve_slice` (ADR 0027) to
obtain the real, deterministic derived specification for one
already-approved plan's slice, then calls `VerticalSliceRunner
.execute_approved_task()` directly -- deliberately bypassing
`start_slice`'s durable execution-operation ledger, lease, and
authorization-package machinery (ADR 0024/0025/0026).

This is a documented, argued equivalence, not an unexamined shortcut:
`execution.operation_service.run_execution_operation` itself, after all
of its crash-recovery/drift-detection bookkeeping, does nothing more than
build providers and call the identical `VerticalSliceRunner
.execute_approved_task()`. None of that bookkeeping alters the
specification, context package, configuration, or agent session a live
run actually experiences -- it exists to make a *durable, resumable*
execution safe across process crashes and concurrent submissions, which a
single foreground diagnostic probe does not need. This mirrors
`run_monolithic_condition`'s (ADR 0028) own established pattern of
constructing `VerticalSliceRunner` directly for evaluation purposes.
Bypassing `start_slice` also means this milestone touches zero lines in
`architect/slice_service.py`, `execution/operation_service.py`, or
`execution/operation_store.py`.

Per your instruction, the three preserved D4b planned-condition attempts
(each of which *did* run through `start_slice`'s full path) are treated
as the unchanged-prompt/unchanged-model baseline; no redundant
reproduction probe was added. The equivalence argument above is exactly
what was checked to confirm that treatment remains valid -- if a live
probe result ever looked inconsistent with the D4b baseline in a way this
argument doesn't explain, that would be cause to add the reproduction
probe after all, not to trust the shortcut blindly.

### Deterministic behavior summary

`ProbeBehaviorSummary`/`summarize_diagnostic_probe()` is a pure function
over a session's own persisted `list[AgentTurnRecord]` -- no I/O, fully
unit-testable:

- `invoked_run_check`/`invoked_submit_for_verification`: whether the
  model ever requested that action at all (accepted or not).
- `first_no_progress_turn`: the first turn that is **all three** of --
  accepted, one of `read_file`/`search_repository`/`inspect_diff`, and
  exactly repeats an *earlier* turn's `(action, summary)` pair *and*
  contributes zero new evidence (`evidence_ids == []`). All three
  conditions are required together, not `evidence_ids` alone and not
  repetition alone: a turn's first, novel inspection is never flagged
  merely for adding no evidence (e.g. `inspect_diff` on an untouched
  worktree), and -- the specific bug an early implementation of this
  function had, caught before any live run -- a *repeated* inspection
  that nonetheless adds real new evidence is never flagged either. This
  matters concretely: after an accepted edit, the model's legitimate
  one-time reread of the just-edited file has the *same* `(action,
  summary)` text as its pre-edit read (`summary` only encodes the
  path/line-range, not file content), but a *different*, non-empty
  `evidence_ids` (the content changed). Only a later, truly identical
  repeat -- same text, and now genuinely no new evidence -- counts as the
  first no-progress turn; a regression test
  (`test_a_fresh_post_edit_reread_with_new_evidence_is_never_no_progress`)
  encodes exactly this four-turn sequence. `run_check`/
  `submit_for_verification` are excluded entirely from this definition,
  since verification turns never populate `evidence_ids` regardless of
  outcome (`BoundedAgentSession._record_verification`) and the harness's
  own identical-verification dedup already prevents a literal repeat from
  ever being accepted twice -- a real verification attempt, even a
  failing one, must never be misclassified as "no progress".
- `max_identical_action_streak`: the longest run of consecutive turns
  sharing an identical `(action, summary)` pair -- a normalized-equality
  proxy, not a raw byte comparison of the model's JSON (that level of
  detail remains in the preserved `call-NNN-response.json` artifacts).

`DiagnosticProbeResult` always records `prompt_condition` and
`model.{model,source}` explicitly as top-level fields -- never left
implicit or reconstructable only from a file path -- plus the full
`FinalTaskReport`, `duration_seconds`, and `evidence_kind` (distinguishing
`deterministic_fake` from `live_local`, exactly like every other
evaluation artifact). `diagnostic_probe_report.write_diagnostic_probe_
report()` persists `diagnostic-probe.json`/`.md`, mirroring `evaluation.
planning_report.write_planning_comparison()`'s existing convention.

### CLI

```
apoapsis eval-planning-probe download-service-v2 \
  --plan-id PLAN-... --expected-plan-version N \
  --planned-project-root <already-approved-disposable-project> \
  --slice-id SLICE-... \
  --prompt-condition production|progress_advisory \
  [--alternate-model NAME --authorize-alternate-model NAME] \
  [--output-dir DIR]
```

Requires `--planned-project-root` to already have the plan exported,
imported, validated, and approved via the existing, unmodified `apoapsis
plan ...` workflow, at its untouched baseline -- this command never
generates or approves a plan itself, exactly like `apoapsis eval-planning`
(ADR 0028). It never copies or mutates the harness checkout's own
`.apoapsis/config.toml`; the only config change (an alternate model) is an
in-memory `model_copy`, matching `_apply_context_profile`'s existing
convention.

**Deliberately no `--context-profile` flag** (an experiment-integrity
correction from review): `apoapsis eval-planning` accepts one because it
compares whole conditions that are otherwise expected to differ; this
narrowly scoped single-slice probe exists specifically to isolate one
independent variable, so it always inherits the harness checkout's
baseline `.apoapsis/config.toml` (including context window) completely
unchanged. Allowing a context-profile override here would let a probe
silently vary a second, unrecorded variable alongside the one it claims
to isolate.

## Tests

`tests/test_diagnostic_probe.py` (28 tests, all deterministic fake
providers or pure functions): the advisory prompt is exactly the
production prompt plus one appended, non-forcing note; the behavior
summary correctly detects both the D4b-shaped read loop and a normal
verify-and-complete session, including edge cases (an empty session, a
first novel `inspect_diff` never misflagged, and the corrected fresh-
reread-vs-genuine-repeat distinction above); alternate-model
authorization fails closed for an unauthorized name (without ever
querying installed models), for an authorized-but-uninstalled name, and
succeeds only when both conditions hold, including a bare-tag-matches-
`:latest` case; the alternate-model config clone changes only `.model`;
`validate_single_independent_variable()` is exercised directly for all
four combinations (the one forbidden cell rejected, the three valid
cells allowed); **regression coverage proving the injection point is
inert by default** -- a `BoundedAgentSession` built with no
`agent_step_prompt_fn` argument produces a prompt byte-for-byte identical
to calling `agent_step_prompt` directly, and an ordinary
`VerticalSliceRunner(...)` construction (no new argument, exactly the
existing product/`run_monolithic_condition` pattern) never emits the
advisory note anywhere in its live session prompts; both probe conditions
run end-to-end against a real one-slice plan (built with the existing
`tests/architect_helpers.py` fixtures) with fake providers, proving
`PROGRESS_ADVISORY` prompts contain the note and `PRODUCTION` prompts
never do, on an otherwise byte-identical specification/context/config/
fixture; the read-loop-shaped fake script run through the real
orchestration path produces a `DiagnosticProbeResult` whose behavior
summary matches the forensic findings; the persisted JSON/Markdown
artifacts explicitly record `prompt_condition` and `model.{model,source}`
for both of the two valid combinations (split into two separate tests,
since `PROGRESS_ADVISORY`+`explicit_alternate` is no longer a valid
combination to test as one case), plus a dedicated test that this invalid
combination is rejected end to end through the real orchestration path.
CLI: parser argument coverage, `--context-profile` now correctly
rejected as an unrecognized argument, the mismatched-authorization
fail-closed path, the alternate-model/`progress_advisory` fail-closed
path (both checked before any filesystem access), and the same-
configured-model rejection (checked against a real `apoapsis init`-created
project, the one check in this milestone that legitimately needs to read
configuration first). Full suite: see `HANDOFF.md`'s refreshed snapshot
for the observed count; `python -m compileall -q src tests` and `git diff
--check` both clean.

## Deferred follow-ups (found, not fixed here)

Both were found during the forensic pass and are deliberately excluded
from this milestone's scope so they do not contaminate a controlled
single-variable probe:

- **`search_repository` fails with a raw `[WinError 2]`** on this
  machine: `agent/inspection.py`'s `RepositoryInspector.search()` has no
  fallback when the configured `rg` executable cannot be resolved by
  `subprocess.run(..., shell=False)`, unlike `context/compiler.py`'s own
  `_ripgrep_search`, which degrades to a lexical fallback. Confirmed real
  (`shutil.which("rg")` returns `None` from a plain Python process on
  this machine even though an interactive shell resolves `rg` some other
  way) and confirmed **not** the cause of the read loop (it only ever
  occurred in the three monolithic D4b attempts, each exactly once at
  turn 2, well before the loop begins; the three planned attempts never
  called `search_repository` at all and looped identically). A fix
  belongs in its own reviewed change against `agent/inspection.py`.
- **Stale/fresh evidence duplication after an edit**: a turn's
  `REPOSITORY_EVIDENCE` can show a pre-edit, context-compiler-supplied
  copy of a file alongside the freshly re-read post-edit copy, with
  neither labeled as to which is current (`base_context.evidence` and
  `observations` are deduplicated only by exact `(path, start_line,
  end_line, content_sha256)`, so a changed file's two different hashes
  both survive). Confirmed present in a previously successful, completed
  live session too (`priority-a-64k`), so it is a general, pre-existing
  harness characteristic, not something specific to the D4b fixture --
  and therefore not, by itself, an adequate explanation for why this
  fixture loops and others do not. Worth a staleness label in a future,
  separately reviewed change to `agent/session.py`'s `_context_for_turn`.

## Non-goals

- No live model call, live Ollama interaction beyond a single read-only
  `GET /api/tags` when an actual probe execution requests an alternate
  model, model download, or Ollama lifecycle change in this commit.
- Does not run Probe 3 (the alternate-model probe). Probe 2
  (`progress_advisory`) and one production-condition control run have
  since been run once each -- see "Live evidence addendum" below. Probe 3
  remains explicitly gated on your separate authorization.
- Does not re-run the full monolithic-vs-planned comparison (D4b/D5).
- Does not change `_AGENT_STEP_STATIC_PREFIX`, the action schema
  (`agent/actions.py`), retry budgets, workflow transitions, or
  completion authority. Models remain untrusted proposers; Apoapsis alone
  still validates, executes, or rejects every requested action.
- Does not fix `search_repository`'s `[WinError 2]` defect or the stale/
  fresh evidence duplication -- both recorded above as separate,
  deliberately deferred follow-ups.
- Does not touch `architect/slice_service.py`, `execution/
  operation_service.py`, or `execution/operation_store.py`.

## Consequences

A reviewer or operator can now run a single, minimal, single-variable
live probe -- either the same model under a narrowly revised advisory
prompt, or a different already-installed local model under the unchanged
production prompt -- against the exact same first, dependency-free slice
D4b already exercised, with the prompt condition and model identity
always explicit in the persisted artifact, and a deterministic summary
that states plainly whether real verification was ever invoked. Probe 2
and a production-condition control have since been run once each (see
"Live evidence addendum" below); Probe 3 (the alternate-model probe) has
not. See `HANDOFF.md`/`NEXT_STEPS.md` for the exact proposed commands and
the explicit authorization this ADR does not itself grant for Probe 3.

## Live evidence addendum (2026-07-20)

Per your instruction, no additional live probes were run beyond the two
already completed at the time this addendum was written. Both used the
project's configured `qwen3-coder-next:q4_K_M` on `SLICE-JOBS-001`
(`download-service-v2`):

- **Probe 2** (`--prompt-condition progress_advisory`): 8 turns, one
  `v2-jobs-tests` run (passed), `AC-JOBS-STATE` proven, `COMPLETE`.
  53,039 input / 876 output / 0 cached tokens, 151.4s.
- **Production-condition control** (`--prompt-condition production`,
  same model and slice, run through this same probe infrastructure): 5
  turns, one `v2-jobs-tests` run (passed), `COMPLETE`. 31,965 input / 803
  output / 0 cached tokens, 109.4s.

Both escaped the read loop this ADR's motivating forensic pass diagnosed:
each edited, inspected the diff, invoked the configured verification
command, passed, and reached real slice-level `COMPLETE`. The production
control succeeded **without** the advisory prompt and in fewer turns than
the advisory condition -- these two observations therefore do not support
changing the production prompt (`_AGENT_STEP_STATIC_PREFIX` remains
untouched, exactly as scoped above) or attributing either success to
`progress_advisory`. They do establish that this model can solve and
verify this slice; D4b's read loop is not a hard, unconditional
capability limitation. The contrast with D4b's 0/6 remains unexplained --
possible run-to-run or setup sensitivity is itself unmeasured. Both
observations cover only `SLICE-JOBS-001`, not the full three-slice plan
or the held-out cross-slice oracle, and no completion rate, reliability
rate, or planning advantage is claimed from two single-run observations.
Full detail, including the exact provenance of every figure above: see
`docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`'s own
live-evidence addendum. The two deferred defects above (`search_repository`'s
`[WinError 2]`, unlabeled stale/fresh evidence duplication) remain
unresolved and were not touched by these runs.

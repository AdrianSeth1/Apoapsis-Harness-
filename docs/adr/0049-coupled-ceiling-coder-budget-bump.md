# ADR 0049: Coupled bump to `max_criteria_per_slice` and the bounded coder / frontier coder budgets

- Status: Accepted
- Date: 2026-07-22

## Context

A planning user reported that a strong frontier planner produced a plan in
which one approved slice referenced 13 distinct constraints/criteria, and
`apoapsis plan validate` refused the plan because that slice exceeded the
configured `[architect.ceilings].max_criteria_per_slice = 12`. The user
asked to apply the raise to every future `apoapsis init`, not only to the
project that triggered it.

The shrinker was flagged as a real concern before implementation: a
20-criteria slice does not mechanically fit the existing bounded local coder
budgets (12 turns, 8 patch attempts, 4 verification runs). Independent
criterion-by-criterion implementation rarely completes inside those
budgets when the criterion count rises by a factor of 1.6x. A coupled bump
-- raising both the slice ceiling and the local and frontier coder
budgets -- is what a planner-produces-13-criteria plan actually needs to
*also* be implementable when its bounded local run starts.

## Decisions

1. **`max_criteria_per_slice` rises from 12 to 20**, scope every slice in
   any future initialized project. The other `[architect.ceilings]`
   defaults are unchanged.
2. **`max_work_brief_chars` rises from 2,000 to 3,500**, paired with the
   criteria bump, so the work-brief ceiling still leaves ~175
   characters/criterion available to the planner without silently
   trimming the planner's output. The other `[architect.ceilings]`
   defaults are unchanged: `max_slices = 40`,
   `max_dependency_depth = 15`, `max_suggested_paths_per_slice = 12`.
3. **Local coder budgets rise, coupled to the criteria bump:** `max_turns`
   12 → 20, `max_patch_attempts` 8 → 14, `max_verification_runs` 4 → 7,
   `max_search_results` 20 → 24, `max_read_lines` 240 → 360,
   `max_observation_chars` 48,000 → 72,000,
   `max_transmitted_observation_chars` 24,000 → 36,000. The same scaling
   factor as the criteria bump (~1.6–1.75×) is applied so a 20-criterion
   slice can plausibly complete inside the same one-coder-cycle scope as
   a 12-criterion slice did before.
4. **Frontier coder budgets rise, coupled to the local bump and the
   criteria bump:** `max_turns` 8 → 14, `max_patch_attempts` 5 → 9,
   `max_verification_runs` 3 → 5. The same observation/search/transmission
   caps as the local coder are retained (frontier coder is not expected to
   need the wider observation window because its prompt context is sized
   differently and the same one-package-per-attempt bounded evidence
   ledger applies).
5. **Everything else is unchanged.** Pydantic field bounds in
   `src/apoapsis/config.py` (`ArchitectPlanCeilings`, `AgentLoopConfig`)
   are already wide enough to fit these new defaults; no schema change.
   `src/apoapsis/architect/validation.py` reads the configured ceilings
   rather than embedding any number; no logic change. Model authority is
   unchanged: the model still only proposes a typed action per turn, the
   harness still owns the workflow, the verification, and the completion
   decision. Raising ceilings is configuration; it is not a grant of new
   authority (HANDOFF.md, "Retry, escalate, or complete | Fixed
   controller rules and configured ceilings").
6. **`apoapsis init` writes the new defaults**; existing
   `.apoapsis/config.toml` files are not silently rewritten by this
   change. A project that explicitly wants the old ceiling must keep
   writing `max_criteria_per_slice = 12`; a project that explicitly wants
   the new ceiling must keep writing `max_criteria_per_slice = 20`.

## Non-goals

- **No relaxation of agent authority.** Raising the budget does not let
  the model bypass patch policy, approve its own work, write untracked
  test changes that delete tests, or self-declare completion.
- **No widening of the search/read caps on the frontier coder.** The
  frontier coder is normally given a richer-importer package, not a
  wider observation; bumping its observation caps would inflate every
  frontier call's input token cost without measurable benefit.
- **No widening of patch policy.** `max_changed_lines`, `max_files`,
  `allow_dependency_changes`, `allow_test_changes`, and the verification-files
  allowlist are not coupled to the criteria bump and not changed here.
- **No change to the configured `[verification.commands]`, `[context]`,
  `[research]`, or `[patch]` ceilings** in `src/apoapsis/cli/app.py`'s
  `DEFAULT_CONFIG`. If the larger coder budget calls for a wider context
  window, that is an explicit `--context-profile` or `[context]` choice,
  never coerced by the criteria ceiling.

## Determining the numbers

The criteria ceiling was raised from 12 to 20, a factor of 1.667×. The
coder budgets above use the same factor (or the closest integer):
`max_turns` 12 × 1.667 = 20; `max_patch_attempts` 8 × 1.75 = 14;
`max_verification_runs` 4 × 1.75 = 7; observation char ceilings × 1.5; line
and search counts × 1.5. Each ratio is bounded by the existing
`AgentLoopConfig` Pydantic bounds (`max_turns le 50`, `max_patch_attempts
le 20`, `max_verification_runs le 20` -- see existing schema), so the
new defaults are within the already-accepted schema. Per-criterion
work_brief budget is preserved at ~175 chars, so planner-side pressure
remains equivalent for a borderline case.

## Consequences

Future `apoapsis init` calls write a `[architect.ceilings]` block with
`max_criteria_per_slice = 20` and `max_work_brief_chars = 3500`, and an
`[execution.agent]` block with the larger budgets above. A plan whose
slices reference up to 20 constraints/criteria (or fewer) per slice
validates successfully with the new defaults; a slice referencing 21
still fails closed with `TOO_MANY_CRITERIA`. The same architectural
guard in `src/apoapsis/architect/validation.py` remains in place --
the failure mode is configuration-aware, not hard-coded. A larger slice
now also has the local coder budget to actually finish against the
stronger planner output, so the new default does not silently produce
plans that validate but time out at slice execution.

## Tests

`tests/test_architect_validation.py`: a test that builds a slice with
exactly 21 criteria (one above the new default) still produces the
`TOO_MANY_CRITERIA` finding; a test that builds a slice with exactly 20
criteria validates cleanly. `test_architect_cli` keeps its
`DEFAULT_CONFIG` round-trip assertion with the new coupled values.
`test_vertical_slice`, `test_agent_loop`, and `test_architect_slice`
keep their `ApoapsisConfig()`-construction tests passing without source
edits because they construct configs explicitly or rely on defaults
that already match the new numbers.

## Maintenance

A future bump must keep the criteria ceiling and the local coder
budgets in lockstep. If one is raised and the other is not, slices will
validate but the local coder will likely time out before all criteria
are addressed. The `apoapsis doctor` and `apoapsis inspect` surfaces
already read the effective ceilings and budgets and report them; they do
not need changes here. The previous doctrinal quote "Defaults are 12
local turns with 8 patch attempts and 8 frontier turns with 5 patch
attempts" is replaced by the new quote in `HANDOFF.md`'s bounded-coding
section.

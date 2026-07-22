# Apoapsis Harness: Active Next Steps

This file is intentionally short. It lists current owner actions and coding
priorities only. Current architecture is in `HANDOFF.md`, decision history is in
`docs/adr/`, and observed live evidence is in `docs/evaluation/`.

## For the owner

1. Configure each target repository before execution.
   - Replace the initialized example verification command with the project's
     real checks.
   - Test creation and editing are permitted by default; set
     `patch.allow_test_changes = false` only when the repository's policy
     requires tests to be protected.
   - Dependency manifest edits are permitted by default; set
     `patch.allow_dependency_changes = false` for repositories that require
     dependencies to be owner-managed.
   - In strict mode, explicitly mark only genuinely sufficient commands as
     acceptance commands and map criteria to them.
   - Run `apoapsis doctor` before spending model calls.

2. Use the guided local application.
   - Quick change: create and approve one bounded task.
   - Larger project: clarify, optionally research, import/validate/approve a
     plan, then package and execute one dependency-ready slice at a time.
   - After a completed slice, package the next slice; Apoapsis checkpoints and
     inherits completed prior work without moving the checked-out branch.
   - After the final slice completes, use **Prepare finished project**, then
     download the ZIP. Download the companion frontier-review handoff when a
     whole-project ChatGPT/Claude review is desired.

3. Start the optional research model only when planning research is desired:

   ```powershell
   .\START_APOAPSIS.cmd --include-research
   ```

4. Add hosted-frontier credentials and pricing only when a live hosted run is
   explicitly desired. Hosted evaluation also requires an aggregate maximum
   spend. No live hosted coding claim exists yet.

## Coding-agent priority order

### Priority 1: finish and verify the current working-tree change

- Preserve the existing ADR 0035 guided-workflow/planning-research work.
- Verify ADR 0036 clarification, research allocation/diagnostics, execution
  preflight, patch-budget defaults, and documentation compaction.
- Verify ADR 0038 new-file diff reconstruction and effective patch-policy prompt
  text; these tests were added but intentionally not run at the owner's request.
- Verify ADR 0039 default dependency authoring and plan-local inherited slice
  bases; tests were updated but intentionally not run at the owner's request.
- Verify ADR 0040 required-test-scaffolding obligations and repairable-escalation
  rejection; fake-provider coverage was added but intentionally not run.
- Verify ADR 0041 harness-controlled Python dependency installation; the fake
  backend test was added but no installer or package-index access was run.
- Verify ADRs 0042-0048 covering repair, automatic final verification, complete
  slice context, explicit routing choices, maximum high-risk local execution,
  richer frontier handoffs, and finished-plan delivery; coverage was added but
  intentionally not run at the owner's request.
- Run focused tests, the full deterministic suite, compileall, and diff check.
- Do not make a live network, local-model, hosted-model, Docker, or browser claim
  unless that exact path is separately exercised and recorded.

### Priority 2: make project verification setup deliberate

The known impossible unittest case now fails before model spend. The broader
product problem remains: `apoapsis init` cannot know a blank repository's future
stack or correct acceptance test.

Design a separate, explicit configuration flow that:

- detects existing project ecosystems only from repository facts;
- proposes, but never silently installs or executes, candidate checks;
- requires owner confirmation before changing verification commands or
  overriding the configured test policy;
- previews why a command is development-gating versus acceptance-sufficient;
- remains usable for a genuinely blank repository where no check exists yet.

This changes configuration workflow and requires its own ADR and deterministic
coverage. Do not auto-select a command merely to make execution proceed.

### Priority 3: measure local slice reliability

The 2026-07-20 full comparison was 0/6, while two later same-slice probes both
completed. The model can solve the slice, but reliability and the cause of the
contrast are unmeasured.

Before changing the production prompt again:

- run controlled repetitions with one independent variable at a time;
- include a blank/from-scratch project only after its verification contract is
  valid;
- report patch/verification budget use, action sequence, repeated evidence,
  completion, and acceptance proof;
- keep held-out oracle results out of repair context.

An alternate-model probe and a new full comparison still require explicit owner
authorization because they consume live local resources and change evaluation
scope.

### Priority 4: improve research retrieval quality

ADR 0036 prevents query starvation and improves empty-evidence diagnostics, but
does not establish live quality. Next work should use preserved audit records to
measure:

- candidate relevance per planned question;
- zero-finding source rate;
- official-doc URL/domain configuration failures;
- authenticated versus anonymous GitHub search behavior;
- cache effects and source diversity.

Keep network execution inside restricted adapters. Do not give a model a raw
browser, arbitrary URL fetch, shell, credentials, or direct network access.

### Priority 5: collect missing operational evidence

- Re-run the full deterministic suite cleanly after current changes.
- Repeat supported Windows Start/Stop lifecycle checks when model use is
  authorized.
- Add a live hosted result only with explicit credentials, pricing, and spend
  authorization.
- Revisit native packaging only after the explicit verification-configuration
  flow is settled; never hide or auto-install prerequisites.

## Always preserve

- Models are untrusted typed proposers.
- Apoapsis owns repository/network/tool actions, patch policy, verification,
  workflow transitions, retry ceilings, completion, and audit history.
- The held-out oracle remains separate from repair evidence.
- Manual subscription sites are never automated.
- No autonomous multi-slice scheduler, automatic commit/merge, or model-owned
  project configuration.
- Preserve uncommitted user work and the `substrate-v0.1` tag.

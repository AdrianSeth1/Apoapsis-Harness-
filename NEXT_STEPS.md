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
   - Preferred Windows path: double-click `START_APOAPSIS.cmd`, select the
     initialized Git project, let it start the configured local model service,
     and use the UI that opens.
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
- ADR 0073's evidence-count and ceiling output has been exercised by hand
  against two constructed products, not against a real project run. Record a
  live result the next time a browser product goes through the harness.
- ADR 0077's paired scorer, ceiling classification, frozen Crisis Atlas facts,
  workcell, and model relay are verified: `compileall` clean,
  `test_paired_scoring` 47, `test_workcell` 53, `test_workcell_relay` 54, and
  the **full deterministic suite run across all 65 modules**. It has 12
  failures — `test_acceptance_coverage` 2, `test_desktop_import` 3,
  `test_diagnostic_probe` 2, `test_doctor` 2, `test_planning_evaluation` 3 —
  and **all 12 reproduce identically at commit `0fb4e39`**, before this work,
  verified in a detached worktree. Treat them as the standing baseline, not as
  new breakage.
- Every run used a sandbox-local 3.10 shim for
  `StrEnum`/`tomllib`/`datetime.UTC` because no 3.11+ interpreter was
  installable there. **Re-run on the real 3.12 interpreter**, including those
  12 baseline failures: some may be artefacts of the shim or of running a
  Windows-targeted suite on Linux.
- Run focused tests, the full deterministic suite, compileall, and diff check.
- Do not make a live network, local-model, hosted-model, Docker, or browser claim
  unless that exact path is separately exercised and recorded.

### Priority 2: build a baseline-preserving Qwen Capability Sandbox

The unrestricted Crisis Atlas control falsified the assumption that the current
typed Local Power loop preserves enough of Qwen's coding ability. The first
Slice 2 proposal was incomplete, but the harness then stopped the model because
inherited checks stayed green. The normal Qwen CLI, confined to an offline
disposable container, built a much more coherent product and repaired its own
tests, though it still falsely claimed success over a broken status filter.

Follow
`docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md`. In order:

1. **Done (handoff slice 0).** `apoapsis/evaluation/paired.py` implements the
   two scorecards, `PairedRunManifest`, and four separately reported release
   gates with no combined score field; `apoapsis/models/ceilings.py` makes
   `INPUT_CONTEXT_PRESSURE`, `INPUT_CONTEXT_EXHAUSTED`,
   `OUTPUT_CEILING_TRUNCATION`, `TOOL_OUTPUT_TRUNCATION`, and
   `PROVIDER_ERROR_AFTER_ROLLOVER` first-class and keeps them out of the model
   reasoning failure count; `apoapsis/evaluation/crisis_atlas_facts.py` freezes
   both arms so they rescore with no provider, with Slice 2 labelled both a
   proposal miss and a detection miss. `apoapsis eval-paired` with no arguments
   rescores the frozen arms. **Their verdict is `INCOMPARABLE`** — the sliced
   arm's seed commit was never recorded and its output cap changed mid-run — so
   nothing has been shown superior yet. Coverage in
   `tests/test_paired_scoring.py` was added but intentionally not run at the
   owner's request.
2. **Done (handoff slice 1).** ADR 0077 sets the boundary: ephemeral capability
   inside a disposable workcell, durable authority outside it. It supersedes the
   execution boundary of ADRs 0059 and 0071 without editing either.
3. **Done (handoff slice 2, through slices 2A-2D).** The `apoapsis.workcell`
   package provides the pinned identity, the hardened container lifecycle
   (`--network none`, controller-owned Unix-socket relay as the only egress),
   22 containment probes, nine provider-protocol conformance checks, a one-way
   `stream-json` adapter over Qwen's native loop, an execution-profile identity
   gate, a capability-readiness exercise, and the paired capability spike.

   **Live, 2026-07-30:** containment 22/22, relay readiness passed,
   provider-protocol conformance 9/9, both arms running genuine
   `@qwen-code/qwen-code@0.21.1` at `permission_mode=yolo` with 26 tools and no
   `computer_use__*`, capability readiness ready on both, agent profiles
   identical, both tiny matched tasks passed, spike verdict
   **`CAPABILITY_PRESERVED`**, and `slice3-gate.json` records `allowed: true`.
   See `docs/evaluation/slice-2d-execution-profile-identity-2026-07-30.md`.

   Slice 2C's two arms remain `NOT_MEASURABLE` and their ~940,000 input tokens
   are excluded from model-quality and efficiency scoring: they measured
   genuine Qwen Code launched as a read-only planner, which is an
   execution-profile identity failure, not a capability result.

   Standing caveats: one tiny task at one seed promotes nothing; no compaction
   event fired, so the CLI limit mismatch stays *causally consistent* with the
   Crisis Atlas rollover rather than proven; and `relay.py` still cannot be
   imported on Windows.

4. **Done (handoff slice 3).** `apoapsis/workcell/delta.py` computes the whole
   candidate delta by hashing two controller-materialised trees — never the
   workcell's Git, which the agent may rewrite — and classifies every path as
   production, test, dependency, generated, documentation, or forbidden.
   `apoapsis/workcell/admission.py` applies whole-delta policy, reports every
   violation at once, admits or refuses atomically, and reconstructs a clean
   verifier tree from the approved base plus the admitted entries rather than
   from the workcell. `admit_candidate` calls `require_slice3_unblocked`, so
   Slice 2's evidence gates the first thing Slice 3 does.

   **Live, 2026-07-30**, against the real Slice 2D clones: both arms admitted
   (2 production files, 9 changed lines), `.git` and `__pycache__` in neither
   delta, the reconstructed tree running `run_tests.py` to exit 0, a tainted
   copy refused atomically with both forbidden paths reported together and no
   snapshot written, and the base fingerprint unchanged throughout. See
   `docs/evaluation/slice-3-candidate-delta-admission-2026-07-30.md`.

   Outstanding for later slices: verification still runs in the controller
   rather than in a separate pinned verifier workcell (ADR 0077 Layer 4), and
   the admitted snapshot is not yet bound into the plan graph as an
   authoritative `PlanCheckpoint` (slice 6);
5. **Done (handoff slices 4 and 4B).** `workcell/witness.py` defines versioned
   `StructuredWitness` records and refuses eight ways a witness can fail to be
   evidence; `workcell/acceptance.py` compiles readiness against a
   `SliceAcceptanceContract` and replaces ADR 0069's green-test termination
   with `evaluate_checkpoint`, which takes no command results at all.

   Slice 4B closed the operational gap a review found: `contract_compiler.py`
   compiles contracts from the approved plan before spend;
   `emitters.py` produces witnesses from artifacts **the controller deletes,
   requests, reads, and hashes**, never from a model's coverage claim;
   `behaviour.py` generalises the rule from added files to changed behaviour,
   including new symbols and routes inside modified files; and
   `checkpoint.py::run_checkpoint` is the caller, emitting witnesses against
   the admitted snapshot rather than the workcell.

   The integration test the gap demanded exists: the partial Crisis Atlas Slice
   2 proposal receives `CONTINUE`, the next turn finishes it, and only then
   `COMPLETE` — through the real loop. ADR 0079 records the decision;
   `HANDOFF.md` and `README.md` are updated. 20 new tests, focused set 207.

   Slice 4C closed two authority defects review found. The compiler no longer
   promotes the planner's **advisory** `suggested_symbols` and
   `integration_contract_ids` into mandatory obligations marked intentionally
   unmeasured — which had made a suggestion into a gate no evidence could open;
   interface and integration obligations now come only from owner-approved
   `required_interfaces` and `required_integration_routes`, discharged by
   observed symbols and by routes a witness actually called. And required-command
   success is **derived from usable, fingerprint-bound witnesses** rather than a
   caller-supplied set, so a stale witness cannot open the gate. 217 focused
   tests.

   **Outstanding:** no live run — every test uses temp trees and a fake runner
   writing a real coverage artifact; coverage is not independently re-derived,
   which needs ADR 0077's Layer 4 verifier workcell; `observed_symbols` needs a
   wrapper that reports executed symbol names, so interface obligations are
   expressible but not yet dischargeable in practice; and the route heuristic is
   Python/Flask-shaped with symbol extraction Python-only. See
   `docs/evaluation/slice-4b-witness-emitters-and-checkpoint-loop-2026-07-30.md`
   and `docs/evaluation/slice-4c-advisory-metadata-and-derived-command-success-2026-07-30.md`;
6. **Done (handoff slice 5), unwired.** `workcell/context.py` gives the stable
   `TaskKernel` -- which *refuses* a timestamp, UUID, or request id at
   construction, because a volatile prefix silently zeroes the cache while
   looking harmless -- the `StateCapsule` that survives compaction, and the
   fixed prompt layout with `check_prefix_stability`.
   `workcell/compaction.py` adds proactive two-tier compaction (mechanical,
   then semantic only if still over target), per-tool output budgets, and
   head-and-tail truncation that refuses to be irreversible.
   `workcell/budgets.py` replaces turn counts with wall time, process time,
   tokens, and no-progress detection keyed on the worktree fingerprint; the
   call ceiling survives as an emergency stop and refuses to be set low.

   The default 0.70 threshold would have fired at Slice 2D's observed 58,038
   tokens (88.6% of the window), which fired nothing at the time. That is a
   statement about the policy, not about what the model would then have done.

   See `docs/evaluation/slice-5-context-compaction-and-budgets-2026-07-30.md`.

7. **Done in code (handoff slice 5B, ADR 0080), unproven live.**
   `workcell/session.py` supplies the caller slice 5 lacked and corrects three
   authority defects. Prompt stability is provenance, not lexical shape: the
   kernel is written once and read back, so a fixed UUID in an objective is
   legitimate and a mid-session edit raises `KernelDriftError`. Compaction and
   the token ceilings read provider-reported usage only; the estimate is kept
   for diagnosis and barred from both gates, and a missing ledger leaves the
   ceilings `unenforced` rather than passing. Progress is a changed worktree, a
   newly discharged obligation, **or** a new controller-produced evidence
   artifact -- a debugging turn that edits nothing still counts -- while model
   narration never does.

   **Outstanding, and these are the claims the slice exists for.** Two of the
   seven exit criteria are unmet, both live: whether a real Qwen retains the
   capsule and keeps editing and testing after compaction, and whether the
   stable prefix actually earns cached input against this provider. The prefix
   is proven *stable*; it is not proven to *help*, so the efficiency claim does
   not exist. Semantic compaction is a callable with no production
   implementation, so today every over-threshold session mechanical compaction
   cannot rescue stops rather than summarising.
   `TurnResult.observation` is not yet routed through `bound_observation`. See
   `docs/evaluation/slice-5b-session-coordinator-2026-07-30.md`;

7. **Done and QUALIFIED LIVE (handoff slice 5C, ADR 0081 superseding 0080).**
   The probe of the pinned 0.21.1 settled who owns context: Qwen does.
   `qwen --resume <id> -p` restores conversation history, tool outputs and
   chat-compression checkpoints, so Apoapsis injects a bounded handoff capsule
   between native invocations instead of managing the model's history.
   `NativeContextPin` pins `context.autoCompactThreshold` (0.85) and
   `maxRecentFilesToRetain` (5) rather than reimplementing Qwen's ladder, and
   `compaction.py` is now capsule construction and simulation, not the live
   history manager. The claim that its 0.70 default "matched Qwen Code" was
   false and is corrected everywhere: that setting is REMOVED in 0.21.1 and
   silently ignored.

   **Live, 2026-07-30, one run through the controller-owned relay**
   (`tools/slice5c/`, provenance in
   `.apoapsis-eval/slice5c-2026-07-30/provenance.json`): containment 22/22 with
   0 breaches and 0 unproven; the workcell could not resolve the upstream at
   all (`socket.gaierror`, no DNS in the netns); relay readiness ready with 3
   observed requests, and every model turn produced non-zero relay traffic.
   **`--resume` preserves the execution profile** -- `permission_mode=yolo`,
   26 tools, no `computer_use__*`, no tool-search surface -- which had only
   ever been established for a fresh `-p`.

   **Three native compaction events were observed**, as the CLI's own events
   rather than inferred from token counts. **The dependent edit after
   compaction is verified:** `multiply` written against the `subtract` added
   before compaction, both asserted, and the tests run by the controller rather
   than believed from the model's report. **The cache benefit is measured at
   2,173 tokens** for this workload: the stable arm's cached input rose
   19,742 -> 21,915 -> 21,915 at a constant 22,431 input tokens while the
   perturbed arm stayed flat at 19,742. This is the first efficiency number in
   the programme that is a measurement rather than an abstention.

   **Outstanding, and none of it cosmetic.** `context.autoCompactThreshold` was
   never read back from resolved CLI settings, so
   `NativeContextPin.resolved_from_cli` is `False` and 0.85 remains this
   model's default rather than an observed value. One perturbed call shows an
   unexplained 53,397-token second internal call against 33,431 elsewhere; it
   does not touch the measured first-message comparison and is not accounted
   for. And 2,173 tokens is one workload at one prefix size on one server --
   it shows the mechanism works and is observable here, **not a general
   saving**. See `docs/evaluation/slice-5c-live-qualification-2026-07-30.md`;

8. benchmark safe LSP feedback, adaptive verification, task-routed reasoning,
   read-only parallelism, and the local `llama-server` profile without lowering
   any paired quality result. **Task 4 (telemetry) is implemented; the other
   five tasks are untouched and must stay in the handoff's experiment order,
   because each of them changes behaviour this telemetry measures.**

   Task 4 closed the read-back half of the native-context item and found a
   second cause the Slice 5C record did not name: the installed settings
   document writes no `context` block at all, so there was never a configured
   threshold to read and `NativeContextPin`'s 0.85 was an unverified belief
   about the CLI's own default. `pin_capture.parse_native_context` now captures
   the value with provenance and fails closed to `resolved_from_cli = False` on
   any unresolved field. **It has been run** against the pinned image with no
   network, no model call and no setting written: all three fields are
   unresolved and `resolved_from_cli` correctly stays `False`, because the
   bundle exports no default-threshold symbol to fall back to.

   **A correction that outranks the above.** 0.85 is `DEFAULT_PCT`, a
   percentage — not the trigger. `getAutoCompactThreshold()` returns
   `undefined` when unset, and `computeThresholds` takes `min(pct * window,
   window - 20_000 - 13_000)`. At the pinned 65,536 window the absolute ceiling
   wins: the real auto threshold is **32,536 tokens (ratio 0.4965)**, roughly
   half the window. `tools/slice5c/qualify.py` computes `trigger =
   auto_compact_threshold * limit` = 55,706, **1.71x too high**; Slice 5C saw
   its three compaction events only because the real threshold fires earlier.
   **Fixed, and recorded as ADR 0082**, which supersedes only the
   threshold-modelling portion of ADR 0081. `computeThresholds` is exported, so
   it is now executed rather than reimplemented, and its constants are recovered
   by probing it. `WorkcellPin.threshold_ladder` carries all six quantities plus
   the governing term and the source chunk hash; `PIN_SCHEMA_VERSION` is 1.2, so
   pre-ladder manifests are not comparable. `qualify.py` raises rather than
   synthesising a trigger from a percentage. Evidence:
   `.apoapsis-eval/slice5a-2026-07-30/native-context-capture.json` and
   `threshold-ladder.json`.

   **Only the predicted-trigger claim is withdrawn from Slice 5C.** Native
   compaction was directly observed and continuation succeeded; neither result
   depended on the prediction.

   **Binding on task 6 when it starts** (ADR 0082): total token cost from CLI
   session aggregates; per-call and cache comparisons from exposed provider
   messages; the unattributed residual reported separately, always; and the
   aggregate never counted as another call nor its cost omitted.

   **Two things remain owner decisions, not coding
   tasks:** whether to write `context.autoCompactThreshold` explicitly into the
   settings document (it would resolve the pin immediately and would also
   change compaction behaviour), and running
   the full deterministic suite plus `compileall` and `git diff --check` on
   Python 3.11+ — none of the three could run in the session that wrote this
   code, and `tests/test_acceptance_coverage.py` failures observed under a 3.10
   shim are believed environmental but are **not** proven to be.

   **The 53,397 figure was misdescribed and its evidence is retained.** The
   stage-7 records were on the Docker Desktop VM disk and are now durable under
   `.apoapsis-eval/slice5c-2026-07-30/evidence/`. 53,397 is the `result`
   session aggregate, not a second call; the invocation exposed one message at
   22,433, leaving a **30,964-token unattributed residual**. That residual is
   present in all six stage-7 invocations at ~10,997 in five of them, so it is
   structural — the CLI spends tokens on traffic it emits no envelope for — and
   only `perturbed-1` deviates, at 2.82x the cohort median. No cause is
   inferred. `call_decomposition.py` models the aggregate separately from the
   calls it totals and reports the residual; the 2,173-token cache result is
   unaffected and is now pinned by a regression test against that evidence.

   The residual is **persisted and terminally unexplained**, and by owner
   decision it stays that way. **Do not investigate it further unless it
   invalidates scoring** — that is, unless a residual makes a per-case verdict
   or a median-input comparison unsafe under the ADR 0082 accounting rules. It
   is accounted for, not understood, and that is sufficient. See
   `docs/evaluation/slice-5a-telemetry-and-resolved-settings-2026-07-30.md`;
9. make local, genuinely stronger frontier, and human repairs authoritative
   plan checkpoints; and
10. run paired qualification plus architectural negative controls before any
   default changes.

### Slice 5 is FROZEN as of 2026-07-30 (owner decision)

Context work is done. **No Slice 5D. No further threshold archaeology. No
further pursuit of the 53,397 residual** unless it invalidates scoring.

Slice 5A tasks 1-3 and 5 collapse into **one minimal diagnostics and runtime
profile** — enough to keep the agent working and the runs comparable, and no
more. This is deliberately not an optimisation research programme: LSP feedback,
adaptive verification, read-only parallelism, reasoning-effort routing and
`llama-server` tuning are each worth a benchmark only if they plausibly move the
per-case verdict. If one does not, record the negative result and move on.

The remaining path, in order:

1. minimal diagnostics and runtime profile — **DONE, ADR 0083.** Advisory
   syntax diagnostics inside the workcell, captured as controller evidence and
   structurally unable to authorise completion: `DiagnosticReport` is not a
   `StructuredWitness`, `evaluate_checkpoint` keeps its
   `(admitted, detail, readiness)` signature, and `run_checkpoint` collects
   diagnostics *after* deciding. A missing or crashed tool yields `NOT_CHECKED`,
   which is not `CLEAN`. One pinned `QUALIFIED_PROFILE` from the already-
   qualified Slice 5C configuration, trigger 32,536 rather than a percentage.
   Seven optimisations considered, five rejected without benchmarking, two kept
   as candidates (reasoning-effort routing, LSP beyond syntax) — neither
   enabled nor benchmarked. No sweep was run. See
   `docs/evaluation/slice-5a-minimal-profile-2026-07-30.md`;
2. authoritative Codex / frontier / human repair checkpoints — **DONE, ADR
   0084.** `workcell/plan_checkpoint.py` makes a repair a state transition
   rather than an edit, with one shape for all three actor classes: bind, apply
   in controller state, admit, witness, readiness, required verification, append.
   Five bindings (parent, commit, fingerprint, contract digest, failure packet)
   checked *before* anything is applied; nine distinct refusals, because a stale
   proposal and a verification failure need different responses. A human repair
   is not exempt from verification. A failed verification appends nothing, so
   the head does not move. The ledger is append-only and refuses a parent that
   is not the head. `authoritative_delivery_input` and `next_slice_base` return
   the *same object*, and delivery raises `StaleProjection` when handed a
   pre-repair fingerprint. 9/9 required cases plus 16 boundary cases. See
   `docs/evaluation/slice-6-authoritative-repair-checkpoints-2026-07-30.md`;
3. paired corpus, the Crisis Atlas must-pass regression, and negative controls —
   **BLOCKED at Slice 7 Phase 0.** The full deterministic suite now runs on
   supported Python 3.12: **6 failed, 1625 passed**, against the `d50ddf2`
   baseline's **6 failed, 1546 passed** — an identical failure set, so the
   committed work adds 79 passing tests and no new failures. But the baseline is
   not clean, and three of the six are safety rules that are not firing:
   `.git` and `.apoapsis` are not excluded from desktop import, and an absolute
   destination is accepted. They reproduce on Linux **and** Windows, so they are
   not a platform artifact. Two more are the stale-worktree-digest and
   untracked-new-file cases — the exact stale-evidence property Slice 7 would be
   measuring. The nine `enterContext` failures are confirmed **interpreter-only**
   (zero on 3.12). Separately, the suite **cannot run on Windows at all**
   (`ThreadingUnixStreamServer`), so it has never been observed green in any one
   environment and the supported platform is undocumented. No live inference was
   run. See `docs/evaluation/slice-7-phase-0-freeze-2026-07-30.md`.

   **Phase 0B repaired four of the six** as pre-qualification baseline fixes,
   not Capability Sandbox wins: the absolute-destination check ran *after* the
   normalisation that removed the leading slash; `.git`/`.apoapsis` exclusion
   was evaluated against the destination basename, so a file picked from inside
   `.git` lost its parent before the check saw it (walked directories were
   pruned correctly, so only the operator's most likely path was exposed); and
   the read-loop detector required `accepted`, which excluded every turn of a
   loop the harness now *refuses*. `relay.py` no longer subclasses
   `ThreadingUnixStreamServer` at import, so Windows collection succeeds instead
   of aborting the whole run. Linux + Python 3.12 is now **2 failed, 1629
   passed**. Exactly one expectation was changed, justified individually: a
   `>= 4` streak bound that the product's own three-strikes stop rule makes
   unreachable, pinned to `== 3`.

   **Phase 0C corrected the two stale-evidence tests** after owner review. They
   now assert the invariant as the sequence it is: digest-A evidence cannot
   prove digest B (asserted against `compute_acceptance_coverage` itself); the
   criterion is visibly unproven for digest B before the sweep; the sweep may
   re-run and produce digest-B evidence (the mapped command appears twice in
   `verification_results`, the proving run strictly post-dating the mutation);
   and success cites that new evidence. Plus: the digest must genuinely have
   moved, and a pass for a non-acceptance command still cannot prove the
   criterion. Digest matching, the final sweep and the three-strikes rule were
   not weakened, and no product code changed in 0C.

   **Linux + Python 3.12 is green: 1631 passed, 11 skipped, 0 failed.**
   `compileall` and the CRLF-aware diff check pass. **Phase 1 is unblocked.**

   **Windows, recorded accurately:** relay collection succeeds and Unix-only
   cases skip correctly (37 passed, 20 skipped for that module), but full
   Windows execution **stalls around 4% and is not a qualification pass**. Not
   investigated in this phase by instruction. See
   `docs/evaluation/slice-7-phase-0b-baseline-repairs-2026-07-30.md`.

   **Phase 1 froze the qualification manifest (ADR 0085).** Source under test is
   `ad13cf0`; the manifest is
   `docs/qualification/slice7-qualification-manifest.json`, digest
   `8c374827aa4ace9576ed9d2d2f0db04747f3b4fb05d425b10e6fc770454f3762`. Two arms
   (control and Capability Sandbox, no legacy arm, neither with host authority),
   8 corpus cases x 3 repetitions = **24 paired executions, 48 arm-runs**, 10
   negative controls each with a mapped detector, the 15 Crisis Atlas must-pass
   requirements, 9 stop conditions, and Phase 0 provenance recorded with
   `counts_as_capability_sandbox_win = False`. Every model is frozen; the digest
   excludes `manifest_commit` so committing the artifact cannot change it.
   **`ready_for_inference()` is false: 8 controlled-variable hashes still carry
   capture placeholders** and Phase 2 must refuse to start until they are taken
   live. No inference has been run and `llama-server` has not been started.

   **Phase 1B is BLOCKED at the corpus seeds, and no manifest is authorized.**
   Only **1 of 8** required corpus kinds has a real seed repository (Crisis
   Atlas, at `.apoapsis-eval/slice-e-crisis-atlas-seed-2026-07-29`). Focus
   Orbit, cross-file refactor, launch/operability, misleading inherited suite
   and the held-out repository **do not exist**; `examples/download-service{,-v2}`
   are plausible candidates for small-backend-change and test-repair but are not
   declared seeds. **3 of 24 pairs have a concrete seed identity.** The draft
   manifest's per-case hashes are `sha256("slice7::<case-id>::...")` — hashes of
   labels — so resolving the eight placeholders alone would yield a manifest
   reporting `ready_for_inference() == true` that still cannot seed a run. The
   model GGUF and `llama-server` binary were located and are hashable, but were
   deliberately **not** captured, because finalization is atomic and a
   four-of-eight commit produces a third digest that is neither authorized nor
   the baseline. Unblocking is real engineering, not hashing: author seven seed
   repositories with tasks that exercise the capability each case name claims,
   then their task/criteria/command artifacts, then the canonical structured
   mount/verification/argv/repair objects. See
   `docs/evaluation/slice-7-phase-1b-blocked-2026-07-30.md`.

   **Scope narrowed to a Crisis Atlas pilot (owner decision).** The eight-case
   corpus is **deferred, not cancelled** — it remains required before Apoapsis
   can become the default or claim broad non-inferiority. The pilot is one
   case, three repetitions, two arms, six live arm-runs, and Crisis Atlas
   influenced harness development, so it is a **regression benchmark rather
   than held-out evidence**. A passing pilot supports only: *Apoapsis preserved
   or improved Qwen's performance on the Crisis Atlas workload while adding
   measurable protection against false completion.*

   **Slice 7P.1a is done: artifact-backed resolution.**
   `qualification/artifacts.py` closes the false-readiness defect found in
   `cfe7df7`. A digest resolves only when the path exists, is a regular file,
   stays inside the package root *after* following symlinks, is read byte by
   byte, recomputes to the declared value, and matches its declared kind.
   `ResolvedArtifact` is constructible only by `resolve_artifact`, so
   "resolved" cannot be faked by assembling the model. `ArtifactKind` marks
   evaluator-only kinds, so an oracle and a task text stop being
   interchangeable UTF-8 files. `is_label_derived` recognises the draft's
   `slice7::<case-id>::…` scheme for an accurate error, and a test asserts it
   is a message improvement rather than the defence — an unrecognised label
   hash still fails at the missing-file step.

   **Slice 7P.1b is done: one authored, deterministically validated case.**
   `docs/qualification/pilot/crisis-atlas/` carries all twelve required
   components, every one artifact-backed and digest-resolved, and
   `qualification/case_package.py` validates it as eight separately-stated
   proofs. Each proof is `passed`, `failed`, `unrun` or `inconclusive`; only
   eight distinct passes register a package, and `unrun`/`inconclusive` block
   it, because a boolean would force "did not run" to be reported as either a
   pass or a lie.

   Two recoveries changed what the package could claim. The **incomplete
   candidate is the actual historical bytes**, not a reconstruction: the Qwen
   Slice 2 proposal survives in
   `.apoapsis-eval/slice-e-crisis-atlas-64k-codex-slice2-2026-07-29/.apoapsis/tasks/TASK-CB6141309D6E/`,
   one write to `services/incident_service.py`, `finish_reason` `stop`, and a
   verification record showing four configured commands at exit 0. The
   **known-good reference is not** — it is evaluator material derived from the
   Slice 4B turn-two fixture and is labelled as such everywhere it appears.
   The preserved worktree in that same directory holds `service/` (singular)
   with an export service and a full suite; that is the post-Codex-repair
   state and is deliberately unused.

   **Slice 7P.1c supplies the real evidence, and corrects 7P.1b.** `918bc82`
   reported eight passing proofs and a registerable package from a run against
   an **injected fake probe** — nothing cloned, no command run, no witness
   emitted. That validated the validator; the package was **NOT YET
   REGISTERABLE** there. `EvidenceKind` now separates `ORCHESTRATION_ONLY` from
   `REAL_QUALIFICATION`, the probe must declare it, and `registerable` requires
   the latter, so a fake-probe pass cannot be reported as qualification again.
   `real_probe.py` drives the existing checkpoint/witness machinery over fresh
   clones, and **all eight proofs now pass on real evidence** — including the
   historical candidate reaching `CONTINUE` with the acceptance command green,
   and two independent clone runs producing identical candidate fingerprints.

   **Canonical suite results are Linux/ext4/CPython 3.12 with the venv
   activated: 1756 tests, zero failures, 12 skipped.** `/mnt/c` results are a
   portability finding and never the qualification result.

   **Known portability weakness, deliberately not fixed here.** 25 fixture
   sites across 16 test modules configure verification commands as
   `argv=["python", ...]`. Ubuntu ships no `python`, so running the suite via
   `.venv/bin/python` by absolute path — rather than activating the venv —
   leaves it off `PATH` and fails `test_context_measurement_integration` and
   `test_specification_correction` for a purely environmental reason.
   `sys.executable` would be correct everywhere. Repairing 16 modules is not
   qualification work and would mean editing tests to suit a runner; it is
   recorded here so the next person does not re-diagnose it.

   **Still to do (Slice 7P.2):** capture model/server/Qwen/workcell identities
   without inference; author the separate Crisis Atlas pilot manifest; bind the
   three paired executions; resolve every controlled variable; write the
   immutable pilot lock. `PackageProbe.run_checkpoint` now has a real
   implementation in `real_probe.py`, offline and model-free; what 7P.2 adds is
   the *live workcell* path and the runtime identities. The original eight-case
   manifest stays a deferred historical draft: `unresolved_hashes()` = 8 and
   `ready_for_inference()` = false, deliberately unchanged through 7A/7P.1;
4. rollout and fallback **only if** non-inferiority passes.

**Run the full deterministic suite on Python 3.11+ once, before qualification —
not before every implementation step.** The two `test_acceptance_coverage`
failures and the nine `enterContext` failures observed under a 3.10 shim resolve
there or they become real findings; either way that check belongs to
qualification, not to each commit.

### The question every remaining task answers

> Does Apoapsis Qwen match or beat unharnessed Qwen **per case**, with fewer
> false completions and lower median input tokens?

A finding that does not affect that claim, containment, or authoritative state
gets **documented and left**. This file has twice grown a subsection for a
measurement that was interesting and immaterial. Interesting is not the bar.

The release rule is per-case, not merely an average: every task passed by
matched default Qwen must also pass before frontier repair, final verified
quality must improve on at least one case without regressing another, false
completion must fall, and median input tokens must be lower. Until those gates
pass, the Capability Sandbox is experimental and ADR 0071 Local Power remains
a compatibility arm.

### Priority 3: finish the Crisis Atlas remediation evidence

`docs/handoff-2026-07-29-crisis-atlas-remediation.md` specifies five ordered
remediation slices. **Slice A (current-evidence projection) is implemented as
ADR 0072, and slice B (verification-policy semantics) as ADR 0073.** The rest
are outstanding and must stay in order, because the later ones measure
behavior against a contract the earlier ones make coherent:

- **Slice B — done, with one follow-up.** ADR 0073 split
  `--forbid-external-resources` from the new `--forbid-runtime-network-apis`,
  added a shared request-target classifier, and made `verify-web-product`
  report its own evidence counts and ceiling. Outstanding: discovery and
  planning language still does not distinguish "no internet/external assets"
  from "no same-origin API calls", so a plan can still be written that
  requires an integration and configures a check forbidding it. Plan
  validation should surface that contradiction visibly rather than leaving it
  to be discovered at execution time. Fold this into slice C's plan
  cross-consistency validation.
- **Slice C — done.** ADR 0074 added the final integrated verification
  operation, commit/fingerprint binding, the fail-closed delivery gate,
  separate per-slice and whole-project evidence sections, and five structured
  plan-consistency findings including the integration-versus-verification
  contradiction. ADR 0075 then closed its implementation gap: the planner
  handoff now asks for `IntegrationContract.runtime_boundary` by name, and
  enum placeholders in the ADR 0066 literal shape list every permitted value
  instead of the useless default. Full suite run to completion at the
  0072-0074 tree with no new failures. One standing consequence: a plan
  approved before ADR 0074 and not yet delivered cannot be delivered, because
  it names no whole-project verification command. Revise and re-approve such a
  plan; there is deliberately no override.
- **Slice D — done.** ADR 0076 added `launch_verification_command` and
  `launch_not_runnable_reason` (exactly one required), documentation-path
  validation, a delivery-time check that required artifacts are actually in the
  shipped tree, a `DeliveredOperability` record separating "present",
  "exercised", and "explicitly unmeasured", a usage guide that renders the
  plan's structured instructions and labels its filename heuristics as
  inference, and `INTEGRATION_WITHOUT_END_TO_END_PROOF` for networked
  contracts. Outstanding and deliberately not attempted: nothing reads the
  README's *content*, so "the README matches the launch path" is still a human
  judgement; and seed data, demo-only paths, and offline-mode fallbacks remain
  undetectable statically — the lever is forcing a behavioural acceptance
  command to exist, not detecting the smell.
- **Slice E — controlled 32K vs 64K context experiment.** Run only after B-D,
  or it measures behavior against a known contradiction. Keep model-context
  capacity separate from `max_output_tokens` and record results as live local
  evidence. **Partial evidence recorded 2026-07-30:** the Codex-assisted 64K
  checkpoint protocol delivered a verified product candidate, but maximum
  actual input was only 24,583 tokens and the raised output cap—not the context
  window—was the observable benefit. This was not comparable to the autonomous
  32K arm. Authoritative plan delivery/Report agreement remains unrun; see
  `docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`.
  A separate unrestricted-CLI control is now also recorded. The same Qwen model
  built a near-complete product with 88 passing self-authored tests, but used
  about eight times the sliced arm's input tokens, failed the strict web gate,
  and missed a real browser filtering defect. Treat this as action-protocol and
  independent-verification evidence, not as the missing 32K/64K comparison; see
  `docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`.

Then re-run the twelve-point Crisis Atlas regression scenario from a fresh
committed seed, as specified in that handoff.

### Priority 4: retain ADR 0071 as a legacy comparison arm

ADR 0071 is implemented with fake-provider coverage only and makes no claim
about whether it helps. Its original three-arm Focus Orbit comparison remains
useful, but it is now one fixture inside the broader baseline-preserving
qualification rather than the target architecture.

Run the **exact** Focus Orbit challenge three times over:

1. one-action Local Power (`atomic_change_sets = false`);
2. atomic-slice Local Power (the new default);
3. direct one-shot generation against the same endpoint, no turn protocol.

Record per arm: model identity, total turns and calls, files rewritten more
than once, verification runs and refusals, time to a first complete three-file
implementation, owner-test results, `verify-web-product` results, browser
behavior and console errors, a visual-quality review, and which acceptance
criteria are left unproven. Put it in a dated `docs/evaluation/` file and keep
live Qwen results separate from the fake-provider evidence.

Record whether arm 2 improves over arm 1, but do not call it sufficient merely
because it approaches arm 3. The unrestricted Crisis Atlas control established
that the action interface itself can suppress model capability. The new
Capability Sandbox must preserve the normal CLI surface and then outperform it
through independent evidence and repair.

Separately, and independently of the above: the `TASK-A0E17C03D69B`
continuation reached task state `COMPLETE` while `report.json` and the Report
page kept the original `human_review_required` headline. **Addressed by ADR
0072** — `report.json` is deliberately still not rewritten; a shared
current-evidence projection now supplies the outcome to every surface that
labels one. The full deterministic suite has not been run for that change; see
Priority 1.

### Priority 5: make project verification setup deliberate

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

ADR 0069 delivered the reporting half of this, not the flow: contract evidence
level is now computed and shown in Doctor, the authorization package, the
report, and the UI, and `apoapsis verify-web-product` gives browser projects
one real check to configure. The proposal-and-confirmation flow above is still
outstanding. Two follow-ups belong to it:

- **Done, and it produced ADR 0070.** `TASK-E01762481075` ran with
  `web-product-integrity` required: the harness stopped safely at
  `HUMAN_REVIEW_REQUIRED` with no false COMPLETE, but the repair continuation
  could not act because the normalized failure never reached the Local Power
  prompt. Fixed in ADR 0070 (fake-provider coverage only). **The open question
  is now the model, not the harness:** rerun the same task and record whether
  Laguna actually repairs the four unresolved element ids when it is shown the
  failure output, the outstanding-command list, and a refused premature
  `finish`. If it still cannot, that is a capability result about Laguna and
  should be recorded as one rather than met with more harness changes.
- Decide whether a `criterion_mapped` floor should ever be enforceable per
  project. ADR 0069 deliberately only reports; do not turn that into a block
  without its own ADR, since `apoapsis eval` depends on baseline semantics.

### Priority 6: verify the new Start/Laguna local path live

ADR 0062 fixed the deterministic launcher/lifecycle mismatch: Start can now
select a project, manage loopback OpenAI-compatible `llama-server` targets, and
open the UI. This has not been exercised against the owner's real Laguna setup.

Next work:

- set `APOAPSIS_LLAMA_SERVER_COMMAND` to the explicit owner-approved
  `llama-server`/WSL command for Laguna S 2.1;
- run `START_APOAPSIS.cmd`, select a real initialized test repository, and
  confirm the UI opens against the same selected project;
- run one tiny Local Power task end to end;
- record exact live evidence under `docs/evaluation/`;
- do not claim `llama-server` process cleanup support until it is explicitly
  designed and verified.

See `docs/opus-handoff-2026-07-26-startup-and-local-mode.md`.

### Priority 7: measure local slice reliability

The 2026-07-20 full comparison was 0/6, while two later same-slice probes both
completed. The model can solve the slice, but reliability and the cause of the
contrast are unmeasured.

Blocked prerequisite, now partially cleared. The harness-side diagnosis and
fix landed as ADR 0063: changed-path classification, `PYTHONDONTWRITEBYTECODE=1`
in the verification environment, structured-edit EOF normalization, and a
no-progress whitespace guard covering every edit action rather than only
`propose_patch`. Deterministic fake-provider coverage passes
(`tests.test_agent_loop`, `tests.test_verification`,
`tests.test_local_power_session`, `tests.test_cli`).

**Reliability measurement is still gated on the live rerun**, which has not
been performed. Rerun the same tiny subtract task against Laguna in
`C:\Users\aryam\apoapsis-live-test` and record, as a dated
`docs/evaluation/` note clearly labelled live local evidence:

- task ID and exact scratch repository state;
- Local Power setting and route;
- action sequence;
- patch attempts and verification runs;
- rejected tool requests;
- final `files_changed` (must contain no `__pycache__` entries);
- verification output showing both `test_add` and `test_subtract`, or a clear
  `human_review_required` explanation if the model still fails to add the test.

Separately, the full suite now stands at 916 tests with 9 failures and 2
errors, all pre-existing and none caused by ADR 0063 (each was reproduced with
that ADR's behavior changes neutralized). Five of them are first-execution
defects in the never-before-run desktop modules. They do not block the live
rerun, but they should be diagnosed before the suite is treated as a clean
gate again; `HANDOFF.md` has the inventory.

Only after that rerun should broader reliability measurement resume. Background
and the original diagnosis:
`docs/opus-handoff-2026-07-26-laguna-patch-loop-and-review-surface.md`.

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

### Priority 8: improve research retrieval quality

ADR 0036 prevents query starvation and improves empty-evidence diagnostics.
ADR 0055 fixes the reproduced misleading-provenance-error bug (operation
`DISCOP-796622810B804FE59E87536D`): classified failure reasons, pre-retrieval
official-doc query-feasibility checks, per-question fair allocation in
`SourceRanker`, one bounded recovery pass on total extraction failure, and a
harness-owned official-doc search-provider seam with no vendor implemented
yet. Its new/updated tests in `tests/test_research_units.py` and
`tests/test_research_integration.py` were added but intentionally not run
this session; run them before treating ADR 0055 as verified. None of this
establishes live retrieval quality. Next work should:

- configure and verify the one implemented `OfficialDocumentSearchProvider`
  (ADR 0056: Tavily, chosen over Brave Search after Brave's free tier
  turned out to require a credit card and metered billing) -- set
  `TAVILY_API_KEY`, add `api.tavily.com` to both allowlists, and run a real
  authorized query before treating official-doc search as working;
- use preserved audit records (now including `unusable-queries.jsonl` and
  `recovery.json`) to measure candidate relevance per planned question,
  zero-finding source rate, official-doc URL/domain configuration failures,
  authenticated versus anonymous GitHub search behavior, and cache effects
  and source diversity;
- measure how often the new bounded recovery pass actually finds evidence a
  first pass missed, versus how often it is a wasted extra model call.

Keep network execution inside restricted adapters. Do not give a model a raw
browser, arbitrary URL fetch, shell, credentials, or direct network access.

### Priority 9: native desktop shell (ADR 0050/0051) -- verify what exists, then wire it up

- Run the still-unexecuted test modules and fold results into `HANDOFF.md`'s
  Snapshot (needs Python 3.11+; this sandbox's default 3.10 cannot even
  import `apoapsis.config`):
  ```powershell
  python -m unittest tests.test_native_shell_spike -v
  python -m unittest tests.test_desktop_registry tests.test_desktop_import -v
  python -m unittest tests.test_desktop_reference tests.test_desktop_home -v
  python -m unittest tests.test_desktop_ipc_server -v
  python -m unittest tests.test_desktop_authority_boundary -v
  ```
- Build and run `spikes/native-shell-tauri/src-tauri` on a real Windows
  machine with a Rust + Tauri 2 toolchain. Confirm no system browser tab
  opens, and record real startup time, packaged size, and failure-dialog
  behavior before treating Phase 1 as closed. (A rootless Linux pass got
  the real Tauri 2 dependency graph to resolve and partially compile through
  glib/gio bindings before a Linux-only GTK3 system-library gap; `main.rs`
  itself has still never been type-checked against the real `tauri` API --
  see ADR 0050's evidence section.)
- ADR 0051 implemented Phase 2 (project registry) and Phase 3 (safe
  import); ADR 0052 implemented Phase 4 (read-only reference-project
  attachment/evidence capture) and Phase 5's backend half (Home-screen data
  service, plus a real but unbuilt `tauri::menu` File/View/Help skeleton);
  ADR 0053 implemented Phase 6's privileged local-IPC channel (second
  loopback listener, same process, own token, fourteen typed routes); ADR
  0054 wired a native picker (`tauri-plugin-dialog`) to every remaining
  menu handler except `show_project_folder`, and filled several Phase 7
  coverage gaps (readiness timeout, import atomicity, one-project-per-
  window, static authority-boundary regression tests).
- **Next, and now the clear bottleneck -- this cannot be done from a
  sandboxed Linux environment; it needs a real Windows machine**: build
  and run `spikes/native-shell-tauri/src-tauri` with a real Rust + Tauri 2
  toolchain. This is Phase 1's original, still-unmet requirement (see ADR
  0050) and blocks everything downstream of it:
  - Confirm no system browser tab opens; record real startup time,
    packaged size, and failure-dialog behavior.
  - Correct whatever `tauri-plugin-dialog`/`tauri::menu` API mismatches
    the real compiler finds (ADR 0054 disclosed these as best-effort,
    unverified guesses at the crate's actual method names).
  - Only then: exercise the full manual checklist in ADR 0050's Phase 8
    (project selection, import preview/collision/replacement/rejection
    cases, reference-project attachment, ordinary task execution
    regression, window-close process cleanup, DPI/scaling).
- Smaller, still-outstanding gaps once real hardware is available: a
  file-tree picker for selecting *which* files become reference evidence
  (today only whole-project attach is wired), and the `show_project_folder`
  "reveal in file manager" action.
- Real HTTP/JS wiring on the *browser-facing* server is **not** the plan --
  the whole point of Phase 6 is that the browser-facing surface must not
  gain filesystem-adjacent capability. The native window's own frontend
  calls the privileged channel directly.
- Never let a model receive a native file handle, an arbitrary path, or a
  filesystem API; only the desktop controller may hold user-granted
  filesystem capability, and only within what the user explicitly selected.

### Priority 10: collect missing operational evidence

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

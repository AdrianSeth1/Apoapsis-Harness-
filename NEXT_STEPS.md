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
- Verify ADR 0077's paired scorer, ceiling classification, and frozen Crisis
  Atlas facts; `tests/test_paired_scoring.py` was added but intentionally not
  run at the owner's request.
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
3. **Next.** Run the default Qwen CLI or a conformance-tested equivalent in that
   workcell. Do not add acceptance repair yet; exit when no useful control
   capability is missing and containment tests show host paths, network,
   credentials, and controller sockets are unreachable;
4. admit and verify the complete candidate delta outside the model's trust
   boundary;
5. replace green-test termination with strict slice-readiness contracts and
   structured witnesses;
6. add bounded recoverable tool output, two-tier compaction, stable-prefix
   prompt caching, persistent state capsules, adaptive budgets, and explicit
   context/output truncation outcomes;
7. benchmark safe LSP feedback, adaptive verification, task-routed reasoning,
   read-only parallelism, and the local `llama-server` profile without lowering
   any paired quality result;
8. make local, genuinely stronger frontier, and human repairs authoritative
   plan checkpoints; and
9. run paired qualification plus architectural negative controls before any
   default changes.

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

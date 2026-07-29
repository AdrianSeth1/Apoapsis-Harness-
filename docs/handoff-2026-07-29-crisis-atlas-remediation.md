# Apoapsis handoff: Crisis Atlas completion and delivery remediation

Date: 2026-07-29

## Assignment

Use the preserved Crisis Atlas run to fix the gap between:

1. a plan whose slices all reach `COMPLETE`;
2. a delivery whose configured checks are green; and
3. a project that is actually integrated, launchable, persistent, documented,
   and truthfully summarized.

This is a harness-remediation assignment, not permission to change Apoapsis's
authority boundary. Models remain untrusted proposers. Apoapsis must continue to
own repository actions, verification, retry ceilings, workflow transitions,
completion, and audit history.

Read `HANDOFF.md`, `NEXT_STEPS.md`, ADRs 0048, 0069, 0070, and 0071, and this
document before implementation. Preserve all uncommitted work and the
`substrate-v0.1` tag.

## Preserved live-local evidence

The evaluation project is:

`C:\Users\aryam\local harness\.apoapsis-eval\crisis-atlas-slice-wrap-2026-07-29`

Important identifiers:

| Item | Value |
| --- | --- |
| Discovery session | `DISC-571F85289FB8` |
| Original plan | `PLAN-237A00567ACA` |
| Approved corrected plan | `PLAN-E1B90639E58D` |
| Slice 1 task | `TASK-15C9E952BCA46C203FCFD93B` |
| Slice 2 task | `TASK-273E095D92CE340272B08171` |
| Slice 3 task | `TASK-B58891206CA7917B31857BE2` |
| Slice 4 task | `TASK-5494B387C75F90D0FDE114A7` |
| Final integrated commit | `9b9fccbae2b1502a0aadbb583544360624569202` |
| Final branch | `apoapsis/5494b387c75f90d0fde114a7` |
| Local model | `qwen3.6-27b`, Q4_K_M, reasoning disabled |
| Configured context | 32,768 tokens |
| Hosted inference | Not run |

The first plan was deterministically rejected because `SLICE-004` referenced
nonexistent `HC-007` and `HC-005` was unrepresented. Owner review also corrected
the test command and moved UI files to the repository root so the configured
web check could see them.

Slices 1-3 completed. Slice 4 used 12 Local Power turns, an automatic frontier
stage, and a four-turn continuation without clearing the last eight web-check
warnings. A hash-bound manual-frontier patch finally repaired those warnings.
All four tasks then reported `COMPLETE`, and plan delivery succeeded.

Current deterministic results at the final commit:

- `python -m unittest discover -s tests -v`: 32/32 passed.
- Configured `apoapsis verify-web-product` invocation: passed.
- Checked-out project `main`: unchanged and clean.
- Finished ZIP: clean of `.git`, Apoapsis runtime state, and credentials.

This is live local inference evidence only. Do not turn it into a hosted-model
claim.

## Product failures the green result missed

Browser and endpoint inspection of the delivered commit established:

1. `python -m api.server` serves `/incidents` but returns 404 at `/`.
2. The dashboard says `Offline Mode`.
3. `app.js` uses in-memory sample data and does not call the backend.
4. A browser-created incident disappears on reload.
5. The backend still returns an empty incident list after browser creation.
6. `README.md` remains the seed README and does not provide a supported
   whole-product launch path.
7. There are no browser-to-API integration tests.
8. After the badge-selector repair, the web checker passed while reporting
   zero element references cross-checked.

Therefore the final commit is a functioning backend plus a separate UI
prototype, not the approved integrated product.

## Harness failures the run exposed

### 1. Delivery reads stale first-stop reports

`src/apoapsis/architect/delivery.py::_report()` reads each task's original
`report.json`. For the final slice that file still says
`human_review_required` and carries the pre-repair failed verification.
`prepare_plan_delivery()` serializes that stale snapshot even though persisted
task state is `COMPLETE` and newer manual-frontier verification passed.

The repository already documents that `report.json` is the original-stop
snapshot. `src/apoapsis/review/case.py::_fresh_evidence()` partially projects
newer evidence for a live review case, but delivery does not use an equivalent
current-state projection.

Do not overwrite or cosmetically rewrite the original report. Audit history is
append-only. Build a single harness-owned projection of current task outcome,
verification, acceptance coverage, and stop/completion reason from task state,
events, and operation artifacts. Use it consistently in:

- the Report page;
- review-case construction;
- finished-plan delivery;
- whole-project frontier handoffs; and
- any API that labels the task outcome.

Add deterministic cases for:

- local continuation: first report is Human Review, continuation completes;
- frontier continuation: first report is Human Review, continuation completes;
- manual frontier: first report fails, applied round verifies and completes;
- verification retry that remains Human Review;
- missing or malformed newer evidence, which must fail closed rather than
  silently substitute an old pass; and
- delivery after each of the successful paths, asserting task outcome and every
  command result come from the same current evidence generation.

### 2. “No external resources” incorrectly forbids the local product API

`src/apoapsis/verification/web_product.py` currently treats every detected
`fetch`/network API as forbidden when `--forbid-external-resources` is enabled.
That collapses two different policies:

- no third-party or internet resource dependency; and
- no browser communication with the product's own same-origin backend.

Crisis Atlas required UI-to-local-API integration, but its configured check
penalized `fetch`. The implementation made the check green by removing the
integration.

Separate these concepts. The likely rule is:

- relative and root-relative URLs such as `/incidents` are same-origin product
  communication and are allowed;
- explicit `http://`, `https://`, protocol-relative, WebSocket, and other
  cross-origin targets remain findings under the external-resource policy; and
- a stricter “no runtime network API of any kind” policy, if retained, must be
  a separately named option with separately documented semantics.

Do not silently change the existing option without an ADR and migration
language. Add focused tests for `fetch('/incidents')`, `fetch('incidents')`,
absolute loopback URLs, third-party HTTPS URLs, protocol-relative URLs,
WebSockets, XHR, and external HTML/CSS assets.

Discovery and planning language must also distinguish “no internet/external
assets” from “no same-origin API calls.” A plan that requires a browser/API
integration contract must not simultaneously turn the required mechanism into
a verification failure without a visible contradiction.

### 3. Plan completion has no integrated whole-project gate

Each slice was verified in isolation. `prepare_plan_delivery()` checks that
every task state is `COMPLETE` and that commits form an integrated ancestry
chain, but it does not execute the plan's
`verification_strategy.whole_project_verification_commands` against the final
integrated worktree before producing the ZIP.

Add a harness-owned final-plan verification operation:

1. resolve the exact integrated commit and worktree;
2. run the owner-approved whole-project commands there;
3. bind every result to the integrated worktree fingerprint and commit;
4. calculate whole-plan acceptance coverage;
5. persist an audit artifact before any delivery state transition; and
6. permit delivery only when the configured final contract is sufficient.

A failed or missing required final command must keep the plan approved but not
executed/delivered and present a clear Human Review path. The model must not
choose, modify, or waive these commands.

Delivery should contain two explicitly different sections:

- per-slice verification history; and
- final integrated-project verification.

Never present the former as proof of the latter.

This workflow change requires a new ADR and deterministic fake-provider
coverage, including an integration defect that no individual slice check can
detect.

### 4. Plans do not make operability and documentation enforceable

The approved plan named a launch command and required a README, but neither was
proven at delivery. The generated usage guide merely recommends reading
`README.md`; it does not establish that the README is current or that one
supported command launches the product.

Strengthen planning and final verification so a deliverable application has:

- one canonical launch path;
- a README containing install, launch, test, persistence, and export guidance;
- a launch smoke test or an explicit owner-approved reason it cannot be run;
- a reachable product entry point;
- no seed, placeholder, demo-only, or offline-mode behavior where the plan
  requires the real backend; and
- an end-to-end scenario mapped to an acceptance command.

Use the plan's structured `delivery_contract`, especially
`launch_or_usage_instructions`, rather than inferring the product's entry point
only from file names. Do not make Apoapsis execute arbitrary prose. The
canonical launch command must still be an owner-approved structured
verification command.

Plan validation should reject or require correction when a required delivery
artifact or end-to-end obligation is not assigned to any slice or final
whole-project command.

### 5. The web checker can pass with negligible evidence

The badge repair replaced computed classes with data attributes. The resulting
check passed with zero element references cross-checked. That can be a valid
static result, but it is weak evidence and should not look equivalent to an
actual UI behavior test.

Extend verification-contract reporting to include web-check evidence counts:

- HTML/JS element references checked;
- CSS selectors checked;
- local assets resolved;
- same-origin API references identified;
- dynamic references skipped or unproven; and
- end-to-end browser behavior measured or unmeasured.

Keep `verify-web-product` deterministic and dependency-free. Do not turn it into
a fake browser. Its report should state its ceiling clearly, and acceptance
configuration should require a stronger project-specific command when criteria
include persistence, browser/API integration, or interaction behavior.

### 6. Context was adequate early and nearly exhausted late

The 32K context window was not uniformly comfortable:

| Task | Calls | Maximum input | Maximum input + output |
| --- | ---: | ---: | ---: |
| Slice 1 | 1 | 4,544 | 5,452 |
| Slice 2 | 6 | 11,544 | 13,534 |
| Slice 3 | 6 | 20,470 | 23,406 |
| Slice 4 | 30 | 32,232 | 32,370 |

Slice 4's frontier/repair history repeatedly reached 30-32K input tokens. Three
early responses separately hit the configured 8,192 output-token ceiling.
Context pressure is therefore plausible, but it is not an explanation for the
contradictory check, stale delivery metadata, missing final gate, or absent
integration tests.

Run a controlled 32K-versus-64K comparison before changing defaults:

- same model file, quantization, endpoint options, seed project, approved plan,
  slice package, action budgets, and verification contract;
- one arm at 32K and one at 64K;
- record prompt input, output, stop reason, output-cap hits, retained evidence,
  compaction events, repeated actions, time, verification runs, and outcome;
- inspect whether 64K preserves useful architecture/failure evidence or merely
  retains repetitive transcript noise; and
- keep model-context capacity separate from `max_output_tokens`.

Add an operator-visible warning when a call exceeds a documented fraction of
its context window or hits its output cap. Do not automatically expand the
window: memory cost and supported context remain owner-configured facts.

If compaction changes, retain these in priority order:

1. current approved slice and integration contracts;
2. current repository state and changed-file summaries;
3. latest normalized failing command output;
4. outstanding required commands and acceptance obligations;
5. rejected/no-progress action summaries; and
6. only then older conversational/action detail.

Prompt or compaction changes require deterministic transcript coverage and a
new ADR if they change the established context policy.

## Recommended implementation slices

### Remediation slice A: current-evidence projection

Fix stale outcome/verification projection first. It is independently
reproducible, narrow, and affects operator trust. Do not combine it with context
or web-policy changes.

Exit criteria:

- successful continuation/manual repair is shown as current `COMPLETE`;
- original stop evidence remains preserved;
- Report, review, delivery, and handoff agree; and
- malformed or stale evidence cannot become a current pass.

### Remediation slice B: verification-policy semantics

Separate external resources from same-origin product API calls, improve evidence
strength reporting, and update configuration/CLI/Doctor documentation.

Exit criteria:

- `/incidents` fetch is allowed by the external-resource policy;
- a third-party URL is still rejected;
- policy names and upgrade behavior are unambiguous; and
- no generic static check claims to prove browser persistence.

### Remediation slice C: plan consistency and final integrated verification

Add plan cross-consistency validation and a final integrated verification
operation before delivery.

Exit criteria:

- an intentionally cross-slice integration failure blocks delivery;
- final checks are commit/fingerprint bound;
- final-plan and per-slice evidence are displayed separately; and
- no automatic merge, commit, command selection, or model-owned completion is
  introduced.

### Remediation slice D: operability contract

Make launch/readme/end-to-end obligations explicit in plan coverage and final
verification. Update the generated usage guide to render verified structured
delivery instructions.

Exit criteria:

- a delivered application has one tested or explicitly unmeasured launch path;
- the README matches that path;
- the application entry point is reachable; and
- the delivery record states exactly what was and was not exercised.

### Remediation slice E: context-headroom experiment

Run the controlled 32K/64K comparison after A-D make the target contract
coherent. Otherwise the experiment would measure behavior against a known
contradiction.

Exit criteria:

- evidence supports keeping 32K, moving the recommended profile to 64K, or
  changing compaction;
- output-cap effects are reported separately;
- no default is changed solely because hardware has spare memory; and
- results are recorded as live local evidence, not fake-provider or hosted
  evidence.

## Required regression scenario

Re-run Crisis Atlas from a fresh committed seed after the remediation:

1. discovery distinguishes no external internet from same-origin API use;
2. the imported plan validates without owner repair except for genuine design
   choices;
3. the dashboard uses the HTTP API;
4. creating an incident in the browser changes backend state;
5. the incident survives browser reload and server restart;
6. status, timeline events, and action items round-trip through the API;
7. JSON and Markdown exports are deterministic;
8. the canonical launch command serves both UI and API, or the README clearly
   documents an intentionally coordinated two-process launch that is tested;
9. the final integrated verification runs after all slices;
10. delivery is blocked if the UI is switched back to offline/in-memory mode;
11. the final Report page and `delivery.json` agree on the current outcome and
    verification; and
12. the ZIP contains the working project and accurate usage instructions but no
    runtime database, model logs, credentials, or Git metadata.

Browser inspection remains evaluation evidence. It does not give a model browser
authority and it does not replace deterministic configured checks.

## Verification and documentation obligations

For each implemented remediation:

1. add deterministic fake-provider coverage for every model-driven branch;
2. run focused tests;
3. run `python -m unittest discover -s tests -v`;
4. run `python -m compileall -q src tests`;
5. run `git diff --check` (and document the repository's existing line-ending
   caveat if it remains relevant);
6. update `HANDOFF.md` with only observed results;
7. update `README.md` for user-visible behavior;
8. update `NEXT_STEPS.md` when priority order changes;
9. add an ADR for each new architectural or configuration decision; and
10. put the fresh live rerun in a dated `docs/evaluation/` record, explicitly
    separating fake-provider, live local, and live hosted evidence.

Do not claim this handoff itself fixes the defects. It is an implementation and
verification contract for the next change.

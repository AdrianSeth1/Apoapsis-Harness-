# Apoapsis Harness: Current Architecture and Handoff

Read this before changing the project. This document is the canonical map of
the system as it exists now. ADRs in `docs/adr/` preserve decision history,
dated files in `docs/evaluation/` preserve live evidence, `README.md` is the
user guide, and `NEXT_STEPS.md` contains only active priorities. Do not copy
historical narratives back into this file.

When documentation and code differ, implementation plus deterministic tests are
operational truth and the documentation must be corrected in the same change.

## Snapshot

| Item | Current value |
| --- | --- |
| ADR 0077 Slice 2 live gate | **Partially proven, blocked at conformance, 2026-07-30.** A native-ext4 Docker workcell passed all 22 containment probes after the sacrificial clone and image were sanitized. The complete relay path then passed health, model listing, and a one-token Qwen3.6-27B completion, with exactly three relay-observed requests and clean teardown. The run found and fixed a stale relay-counter API and Unix-socket group assignment. Linux CPython 3.12 focused coverage passed 156/156, compileall and diff check passed; the owner stopped the full suite to run separately, so no full-suite result is claimed. The live sequence stopped before either quality task because no driver exists for the nine conformance classifiers; all nine correctly remain `NOT_RUN`. Prompt/tool/template pin provenance and automatic clone sanitization are also missing. Slice 2 and Slice 3 remain blocked. Details: `docs/evaluation/slice-2-live-gate-2026-07-30.md`. |
| ADR 0077 Slice 2C conformance and paired arms | **Conformance fully proven live, quality measurement invalid, Slice 3 still blocked, 2026-07-30.** The nine conformance checks passed **9/9** live against `llama-server b10107-c0bc8591e` serving Qwen3.6-27B Q4_K_M, with containment 22/22 and relay readiness first. The two Slice 2B failures are fixed at the source the owner specified: a `generationConfig` override on the selected `modelProviders` entry (`contextWindowSize` 65,536, `samplingParams.max_tokens` 16,384) — Qwen's bundled model table was **not** patched and still reports 1,000,000/64,000, while what the CLI *resolves* is 65,536/16,384, read back by executing its own `loadSettings`/`resolveCliGenerationConfig` inside the image. The whole effective config is captured, credential-redacted, hashed, and folded into the run manifest digest. ADR 0078 replaces the Unicode check's evidence source with a deterministic echo provider reached through the real relay/forwarder path, comparing captured request bytes to parsed response bytes; model transcription accuracy survives as a non-gating metric (still inexact — Qwen retypes `U+2018`/`U+2019` as ASCII). The relay now **refuses** (never clamps) a request whose output budget exceeds the pinned ceiling and records the peak budget observed; the conformance run observed 16 requests, 14 carrying a budget, peak 4,096 against the 16,384 cap, 0 refusals. The paired arms then ran live with no acceptance repair and returned **`CAPABILITY_REGRESSED`**, but that verdict is **not** valid as a capability comparison: the agent CLI in the workcell image exposes no `write_file`, `edit`, or `run_shell_command` at all (57 tools, mostly `computer_use__*`), contradicting the pin's 13 wire-captured tool names, so neither arm could edit a file. Three defects in this slice's own work were found and are recorded: a hand-carried effective-config hash, an event adapter that silently read zero tool calls from a 44-call session (which alone had produced a spurious seven-capability regression), and a control arm killed by shell command substitution in its own prompt. Near-boundary: the control arm reached 58,038 input tokens, 88.6% of the 65,536 window, with **no rollover and no compaction event**, so the limit mismatch is **causally consistent** with the Crisis Atlas rollover and is explicitly **not** called proven or the root cause. Details: `docs/evaluation/slice-2c-limits-envelope-and-paired-arms-2026-07-30.md`. |
| Crisis Atlas unrestricted Qwen CLI control | **Live local evidence, 2026-07-30.** The same Qwen3.6-27B Q4_K_M received the complete approved plan and an arbitrary Bash shell confined to a disposable offline Docker container whose only host mount was the fresh Crisis Atlas seed clone. After one 64K context rollover it built the full stack, added and repaired tests, and stopped normally with 88/88 self-authored tests passing. Independent unit, behavioral, launch, compile, and diff checks passed, but the configured strict web-product gate failed with 10 warnings and browser inspection found a real AC-005 defect: status filtering sent query parameters that the server discarded, so `Closed` still displayed an `investigating` incident. Create/select/status/timeline/action/reload worked. Across 62 successful calls (63 attempted): 2,080,801 input, 35,787 output tokens, 1,052.3 seconds provider latency. This is about 8x the sliced Qwen input, so it did not save input tokens; it did expose substantially better proposal quality than the bounded sliced protocol while reproducing false-success risk. Details: `docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`. |
| Qwen baseline-preserving superiority handoff | **Design assignment, not implemented, 2026-07-30.** The unrestricted control changes the next architecture target: the primary local path should preserve a normal Qwen coding CLI's persistent shell/file/test loop inside a disposable workcell, while Apoapsis retains durable authority over admission, verification, checkpoints, state transitions, promotion, and delivery. The trace proves the Slice 2 Local Power session was terminated by the harness after its first incomplete file because inherited checks passed; it was not allowed to finish its own stated work. The new handoff requires strict slice-readiness contracts, structured witnesses, independent negative controls, real context compaction, a genuinely stronger frontier role, authoritative human/frontier repair checkpoints, separate proposal/detection scorecards, and paired per-case non-inferiority before rollout. Its performance plan also covers native Qwen headless events, bounded recoverable tool observations, stable-prefix/local prompt caching, two-tier compaction, safe LSP diagnostics, adaptive verification, task-routed reasoning effort, read-only parallelism, warm-process/fresh-workcell reuse, and quality-gated `llama-server` tuning. ADR 0071 atomic change sets remain a compatibility experiment, not the target capability surface. Details: `docs/handoff-2026-07-30-qwen-baseline-preserving-superiority.md`. |
| Crisis Atlas 64K Codex-frontier trial | **Live local evidence, 2026-07-29–30.** Qwen3.6-27B Q4_K_M attempted four dependency-ordered slices at 65,536 context; Codex inspected/repaired each checkpoint before the next slice. Final product commit `0d591d7bbf9eebd276df0bc6677f24d19f505f5e` passed 57 unit tests, 8 configured behavioral tests, the real one-process launch smoke, web-product integrity, compileall, diff check, and an interactive browser lifecycle. A deliberate `localStorage` negative control failed the required unit gate. Across 19 Qwen calls: 258,632 input and 55,364 output tokens. The 16,384 output cap mattered (Slice 4 call 1 used 8,213 tokens); maximum input was only 24,583, so 64K was not stressed and no default context change is justified. This checkpoint protocol did not create authoritative plan delivery state: regression point 11 was not run, and the inspected ZIP is a candidate `git archive`, not a harness delivery. Details: `docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`. |
| Last verified | Through ADR 0037 on 2026-07-21, 61 focused tests and the full 722-test deterministic suite passed with 10 expected skips, plus compileall and `git diff --check`. ADRs 0038-0056 are unverified because the owner explicitly requested no test execution (ADR 0049 and later, including 0051-0056, are also blocked by the Python 3.10 environment on the change workspace -- `apoapsis.config` requires Python 3.11+ for `tomllib`); run their documented commands before commit. ADR 0050's `tests/test_native_shell_spike.py`, ADR 0051/0052/0053's `tests/test_desktop_registry.py`/`tests/test_desktop_import.py`/`tests/test_desktop_reference.py`/`tests/test_desktop_home.py`/`tests/test_desktop_ipc_server.py`, ADR 0054's `tests/test_desktop_authority_boundary.py`, and ADR 0055/0056's new/updated tests in `tests/test_research_units.py`/`tests/test_research_integration.py` have likewise not been run in the authoring session at the owner's explicit request; `python -m compileall -q` was run and passed for every changed research/discovery/config/test file, but run the actual test commands before treating any of this as verified. |
| ADR 0059 status | Deterministic fake-provider boundary suite verified on 2026-07-26: `python -m unittest tests.test_local_power_session -v` passed 35/35 with 1 expected symlink-permission skip. The run also fixed first-execution defects found by actually running the suite: whole-file write content now preserves trailing newlines, prompt assembly reads `active_acceptance_criteria` correctly, read evidence uses `FILE_EXCERPT`, budget accounting ignores forbidden/audit paths, review packages include only permitted changed files/diffs, and a no-change session cannot complete. Full suite still not run after ADR 0059/0060. |
| ADR 0060 status | Deterministic config/provider coverage verified on 2026-07-26: `python -m unittest tests.test_cli tests.test_provider_and_specification -v` passed 22/22. The broader touched-module run `python -m unittest tests.test_agent_loop tests.test_cli tests.test_ui tests.test_execution_ui tests.test_provider_and_specification tests.test_local_power_session -v` passed 129/129 with 1 expected symlink-permission skip. `python -m compileall -q src tests` passed. Plain `git diff --check` is still noisy in this checkout because of mixed CRLF/LF working-tree state; `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` passed. No live Laguna run has been performed yet. |
| ADR 0061 status | Deterministic UI toggle coverage verified on 2026-07-26: `python -m unittest tests.test_ui -v` passed 27/27 and `python -m unittest tests.test_execution_ui -v` passed 26/26. The UI exposes a two-step Local Power switch on Models & environment; the server edits only the known local-power config fields, revalidates the full config before keeping it, and refreshes overview/execution previews. Full suite still not run after ADR 0061. |
| ADR 0062 status | Deterministic launcher/lifecycle coverage verified on 2026-07-26: `python -m unittest tests.test_operator_lifecycle tests.test_launcher -v` passed 26/26, and `python -m compileall -q src tests` passed. `START_APOAPSIS.cmd` is now the primary Windows entry point: select or pass one initialized Git project, start configured loopback local coding service targets including `llama-server`, then open the UI. No live Laguna `llama-server` run has been performed yet, and the full suite still has not been re-run after ADR 0062. |
| ADR 0063 status | Fixes the two harness-side defects found in live task `TASK-EF33C00E5BD4` (2026-07-26): reviewer-facing `files_changed` included Python bytecode written by the harness's own verification run, and 13 patch attempts were spent against 1 verification run because the same `replace_text` deterministically synthesized the same `new blank line at EOF` patch (attempts 3-13 share SHA-256 `7F233AFD...`). Adds `apoapsis.repository.changed_paths` classification, `PYTHONDONTWRITEBYTECODE=1` in the verification base environment, structured-edit EOF normalization, and a no-progress whitespace guard that now covers every edit action rather than only `propose_patch`. Deterministic coverage: `python -m unittest tests.test_agent_loop tests.test_verification tests.test_local_power_session tests.test_cli` passed 88/88 with 1 expected skip; `tests.test_cli` re-run alone after its gitignore expectations were updated passed 10/10. The full suite was run on 2026-07-26 (`python -m unittest discover -s tests -v`): 916 tests, 12 skips, 9 failures and 2 errors. **None are caused by ADR 0063** -- every one was reproduced in a scratch copy with this ADR's behavior changes neutralized; see "Known limitations" for the inventory. `python -m compileall -q src tests` and `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` both passed. **No live Laguna rerun has been performed**, so this is fake-provider evidence only; see `NEXT_STEPS.md` for the rerun gate. |
| ADR 0064 status | Found during the 2026-07-26 live run: discovery session `DISC-7D87B2379D8E` dead-ended at step 3 with a bare `Failed to fetch`. Root cause was a project that was `git init`-ed and `apoapsis init`-ed but never committed, so `git rev-parse HEAD` failed; the resulting `GitCommandError` matched no route's `except` clause, escaped into `socketserver`, and closed the connection with no response. Adds a last-resort `_guarded()` handler (any unhandled exception becomes a readable 500 plus a retained traceback, never a dropped connection), `GitRepository.has_commits()`/`head_commit()` with `RepositoryHasNoCommitsError`, and a `prepare_discovery_operation` precondition that refuses with an actionable 409 before any operation record, lease, or model call. Verified: `tests.test_ui` 31/31 and an eight-module regression run 115/115 with 2 expected skips; `compileall` passed. Confirmed against the live project that produced the defect. |
| ADR 0065 status | Found during the 2026-07-26 live run: importing a real frontier plan (10 components, 8 integration contracts, 17 slices) failed with `request body size is invalid`. The UI shared one hard-coded 64 KB transport cap across every route, roughly thirty times below the 2 MB `discovery.max_response_bytes` the configuration said it would accept, so the paste routes could never reach their own domain ceiling. `_read_json_body` now takes a per-route `max_bytes`; the two pasted-response routes derive theirs from the configured domain limit so the two cannot drift apart again, and the error names the actual size and the limit. Control routes keep the 64 KB bound. Verified: seven UI modules 152/152, `compileall` passed. |
| ADR 0066 status | Found during the 2026-07-26 live run: a frontier plan was rejected with eight `extra_forbidden` errors confined to `delivery_contract` and `verification_strategy`. The handoff embedded the full 26 KB JSON Schema, but those two objects existed only behind `$ref`/`$defs` indirection and were named nowhere in prose; every section the Markdown described in prose came back with correct field names. The handoff now emits a fully expanded literal example of the response object before the schema, generated from the models by `apoapsis.specification.skeleton.json_skeleton` so it cannot drift. Schema strictness is unchanged. Verified: `tests.test_schemas tests.test_discovery` 33/33, `compileall` passed. |
| ADR 0067 status | Third failed round-trip on the same 2026-07-26 planning handoff: the pasted response began with a `` `json `` Markdown fence, so `json.loads` failed at character 0 and the error named the position without showing the character. `apoapsis.specification.pasted_json.parse_pasted_json` now strips a UTF-8 BOM and one surrounding code fence -- transport artifacts only -- before parsing, reports what it stripped, and quotes the first 80 characters on failure. Prose preambles and first-brace scanning are deliberately not attempted. Used by both paste importers. Verified: `tests.test_schemas` 15/15 and a six-module run 126/126; `compileall` passed. |
| ADR 0068 status | Live Qwen discovery responses included a leading `<think>...</think>` wrapper before otherwise valid JSON. Discovery now strips only that leading transport wrapper before the existing strict JSON/schema/source-faithfulness checks. Added deterministic fake-provider coverage in `tests/test_discovery.py`; verification pending. |
| ADR 0069 status | Two independent defects found in live task `TASK-33E0EB6476C4` (2026-07-27, Local Power, Laguna S 2.1). (1) A passing verification did not end the Local Power loop: the first check passed on turn 4 and the model spent turns 5-8 emitting the identical `run_verification` request, after which finalization ran the same full check a sixth time. `LocalPowerSession` now keys `command_results` by worktree fingerprint, caches command sets per state, refuses an identical model-requested re-run, stops the loop as soon as every required command has passed for the current state, and reuses that pass at finalization. (2) The owner's seven static tests all passed against an application that did not run -- `app.js` queried four ids `index.html` never defined and `styles.css` styled five classes the markup never carried. Adds `apoapsis.verification.contract` (deterministic evidence-level grading, surfaced in Doctor, the authorization package, the final report, the review package, and the UI; disclosure only, never a block) and `apoapsis verify-web-product` (stdlib-only HTML/CSS/JS cross-reference an owner can configure as an acceptance command). Deterministic coverage: `tests.test_local_power_session` 55/55 with 1 expected skip and `tests.test_verification_contract` 25/25; a nine-module touched run (`test_local_power_session test_verification_contract test_doctor test_cli test_ui test_execution_ui test_execution_authorization test_agent_loop test_verification`) passed 235/235 with 1 expected skip. **Full suite run on 2026-07-27** (`python -m unittest discover -s tests`): 979 tests, 12 skips, 7 failures and 2 errors — exactly the documented pre-existing inventory (`test_acceptance_coverage` 2, `test_desktop_import` 3, `test_desktop_reference` 1, `test_diagnostic_probe` 1, `test_desktop_home` and `test_desktop_registry` 1 error each) and no others; a first full run in the same session showed one additional error that did not reproduce and coincided with a concurrent `git` invocation against the same worktree. `python -m compileall -q src tests` passed and `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` passed on the changed paths. `verify-web-product` was run against the preserved failing worktree (reports exactly the four ids and five dead rules, exits 1) and against `src/apoapsis/ui/static` (exits 0). **No live local-model rerun was performed**; the termination fix is fake-provider evidence only. |
| ADR 0070 status | Found in live task `TASK-E01762481075` (2026-07-27), the ADR 0069 rerun. The harness performed correctly — 6 turns, 3 writes, 2 verification runs, the redundant `unit-tests` re-request refused, no early termination because only one of two required commands had passed, final verification caught `web-product-integrity`'s 4 unresolved ids and 13 dead style rules, outcome `HUMAN_REVIEW_REQUIRED`, `main` clean, no false COMPLETE. The repair continuation then failed: `RVOP-A6A7D5BCB9C14F7B93483C8E` carried the normalized failure but the Local Power prompt did not (`call-007-request.json` names `web-product-integrity` only in the command catalog and contains none of its output), and no `local-power-verification-failure-*.json` existed because `LocalPowerSession` never normalized failures at all. The model reasoned from a history showing only a passing check, re-ran it, and claimed everything passed. Adds: `FailureNormalizer` in the sandbox `_verify` with a `<verification:NAME>` evidence entry and audit artifact; `resume` seeding the prior stage's unresolved failure; `VERIFICATION_STATE_JSON` (four-valued per command: passing-for-current-code, failed, passed-but-stale, never-run) and `OUTSTANDING_REQUIRED_COMMANDS_JSON`; prompt rules that a currently-passing check cannot be usefully re-run; and a bounded `finish` gate refusing completion when a required command has no result for the current state and nothing has changed since the last check (satisfied by any edit or by running the command, capped at 2 refusals, skipped entirely when the sandbox has no changes). `verify-web-product`'s closing line is now `FAILED: ...` so `FailureNormalizer` can extract a root error. Deterministic coverage: `tests.test_local_power_session` 66/66 with 1 expected skip, including 11 new `FailureEvidenceAndRepairTests`. **No live local-model rerun was performed**; this is fake-provider evidence only, and it makes no claim that Laguna can act on the evidence now that it receives it. |
| ADR 0071 status | Live task `TASK-A0E17C03D69B` (2026-07-27, Local Power, `openai_compatible/qwen3.6-27b`; `EXOP-FE26BA8810574A6F9C9F3888` then `RVOP-37315CE9A0184ACC8E77DC03`) completed after 13 turns and 3 verification runs, but Qwen spent its **first six turns replacing `index.html` alone** and the initial eight-turn session ended with **no `app.js`**. The same endpoint given the same brief with no turn/action protocol produced all three files in one response, more coherent and with `web-product-integrity` passing, though it missed repository-specific element ids (owner tests 5/7) and returned a malformed JSON envelope. Diagnosis: proposal *granularity*, not the harness. Adds `propose_change_set` — one turn may propose a coherent multi-file slice (`write`/`delete` only; deliberately no patch operation) that applies completely or not at all. Every problem is reported at once; an invalid proposal is a byte-for-byte no-op; the changed-line ceiling rolls the whole set back; `base_worktree_digest` gives optimistic concurrency against the ADR 0017 fingerprint; a `delete` may not name a path a configured command points at. The harness verifies an applied set itself and ADR 0069 termination ends the session, so a successful change-set session runs verification once. Repair prompts are delta-oriented (`CURRENT_CHANGED_PATHS_JSON` plus "do not regenerate this from the objective"). `atomic_change_sets = false` restores the pre-0071 protocol exactly — the action leaves both the prompt and the grammar — so the one-action arm of the evaluation is real. Deterministic coverage: `tests.test_local_power_session` **93/93 with 1 expected skip** (23 new `AtomicChangeSetTests`, 4 new `ActionProtocolTests`); `compileall` and the whitespace check passed. **No live model run was performed for this ADR**, and the three-arm Focus Orbit evaluation in the ADR has not been run; the Qwen transcripts above are the motivation, not evidence that the change works. Full suite not run, at the owner's explicit request. |
| ADR 0076 status | Crisis Atlas remediation slice D. The approved plan named a launch command and required a README, and neither was proven: the delivered guide's "Read `README.md`; it is the project's primary usage guide" was produced by checking whether that filename appeared in the archive, the README was still the seed, and `python -m api.server` returned 404 at `/`. Adds two structured `PlanDeliveryContract` fields — `launch_verification_command` (the *name* of a configured command, never a shell string, so the canonical launch path stays an owner-approved structured command inside the existing execution boundary) and `launch_not_runnable_reason` — with validation requiring exactly one (`MISSING_LAUNCH_CONTRACT`, `AMBIGUOUS_LAUNCH_CONTRACT`, `LAUNCH_COMMAND_NOT_WHOLE_PROJECT`). `primary_documentation_path` must be set, safe, and assigned to a slice (`MISSING_PRIMARY_DOCUMENTATION`, `UNSAFE_PRIMARY_DOCUMENTATION_PATH`, `UNASSIGNED_DELIVERY_ARTIFACT`). At delivery, `assess_delivered_operability` compares the contract to the integrated commit's `ls-tree` inventory and refuses a plan whose required artifacts are not shipped; the `DeliveredOperability` record on `PlanDelivery` separates artifact presence, launch exercised by a named command, and launch explicitly unmeasured for a written reason. The ZIP usage guide renders the plan's own install/launch/test/readiness text and demotes the old filename heuristics under a heading labelling them as inference. Adds `INTEGRATION_WITHOUT_END_TO_END_PROOF`: a `same_origin_http`/`cross_origin_http` contract with no end-to-end scenario proven by an acceptance-designated whole-project command. That last one is the structural answer to "no offline-mode behaviour" — the harness cannot detect seed data or a demo-only path without the prose inference barred from gates, so it instead refuses to let such a contract exist with only static evidence behind it. **Breaking:** a plan must now name `primary_documentation_path` and exactly one launch field; plans approved earlier and undelivered must be revised and re-approved. `PlanDelivery` gains a required `operability` field. **Observed results, 2026-07-29:** `tests.test_architect_validation` 69/69 (up from 41); a seven-module run (`test_architect_slice test_planning_evaluation test_diagnostic_probe test_architect_slice_ui test_architect_cli test_schemas test_discovery`) 144 tests with 1 failure, that being the documented pre-existing `test_diagnostic_probe` case failing at its own assertion; `compileall` and the CRLF-aware diff check passed. Full suite run to completion — see the "Full suite, ADRs 0075-0076" row. |
| ADR 0075 status | Closes ADR 0074's remaining implementation gap. The contradiction check needs a planner to populate `IntegrationContract.runtime_boundary`, and nothing asked for it: the binding quality requirements still listed the pre-0074 field set, and `json_skeleton` rendered the enum as its first member, so the ADR 0066 literal shape read `"runtime_boundary": "unspecified"` — the one value that disables the check, formatted like an answer rather than a placeholder. Same class of defect as ADR 0066 itself: there the key was missing, here the key is present and its default value quietly defeats a gate. `json_skeleton` now renders every enum as `<one of: a\|b\|c>` (`Literal` still pins its single constant); the binding requirement names the field, enumerates the values, and states what `unspecified` costs; and a dedicated handoff section after the literal shape explains that a contradicting plan is rejected and why such a plan is unsatisfiable rather than merely wrong. No schema, validation rule, or authority boundary changed. **This does not make the check universal** — a planner may still return `unspecified` and that contract still produces no finding, deliberately, since inventing a boundary is the prose inference ADR 0074 avoids. **Observed results, 2026-07-29:** `tests.test_schemas` 19/19; a six-module run (`test_discovery test_discovery_ui test_schemas test_research_units test_provider_and_specification test_intake`) 126/126; `compileall` passed. |
| Full suite, ADRs 0075-0076 | **Run to completion on 2026-07-29** at the working tree containing ADRs 0072-0076: `python -m unittest discover -s tests` reported **1154 tests, 12 skips, 7 failures, 2 errors** in 1073.2s — the same documented pre-existing inventory as the 0072-0074 run below, with 36 more tests and no new failure. `python -m compileall -q src tests` passed and `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` passed with zero output. No live local or hosted claim is made by this run. |
| Full suite, ADRs 0072-0074 | **Run to completion on 2026-07-29** at the working tree containing ADRs 0072, 0073, and 0074: `python -m unittest discover -s tests` reported **1118 tests, 12 skips, 7 failures, 2 errors** in 1050.6s. That is exactly the documented pre-existing inventory and nothing else: `test_acceptance_coverage` 2, `test_desktop_import` 3, `test_desktop_reference` 1, `test_diagnostic_probe` 1, `test_desktop_home` 1 error, `test_desktop_registry` 1 error. A first run of the same tree reported 12 failures; the five extra were all `test_diagnostic_probe` cases whose fixture plan named no whole-project verification command and so was rejected by ADR 0074's `MISSING_WHOLE_PROJECT_VERIFICATION` at `_approve_plan`. That fixture now declares one, which also un-masked the module's pre-existing failure (it had begun failing earlier, at plan approval, instead of at its own assertion). `python -m compileall -q src tests` passed. `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` passed with zero output across the whole repository. No live local or hosted claim is made by this run. |
| ADR 0072 status | Fixes the stale-delivery defect found in the preserved Crisis Atlas run (`PLAN-E1B90639E58D`, 2026-07-29, `qwen3.6-27b` at 32K). Slice 4 (`TASK-5494B387C75F90D0FDE114A7`) stopped at `human_review_required` with a failed verification, was repaired by a hash-bound manual-frontier patch that verified, and reached a persisted `COMPLETE`; `delivery.json` and the whole-project frontier handoff nevertheless serialized the pre-repair `report.json` snapshot. Adds `apoapsis.reporting.current_state`, a single read-only projection of current outcome/verification/coverage/reason from persisted task state, the append-only event history, and the deciding stage's own operation artifact. `report.json` is never rewritten; the original outcome is carried alongside as `original_report_outcome`. Missing, malformed, or unidentifiable current evidence fails closed with empty results and never falls back to the older report, and `prepare_plan_delivery` now refuses such a slice (plan stays APPROVED, no ZIP, no `delivery.json`). Consumers rewired: Report page and task list, review-case construction (its private `_fresh_evidence` and three event tables deleted, `stop_reason_text` now projected too), delivery and the frontier handoff, plan slice status, and `apoapsis inspect`. **Observed results, 2026-07-29:** `python -m unittest tests.test_current_evidence_projection` passed 21/21; `python -m unittest tests.test_architect_slice` passed 32/32 including the 4 new `DeliveryCurrentEvidenceTests`; a twelve-module touched run (`test_review test_review_ui test_review_execution test_review_hardening test_review_frontier_stage test_manual_frontier test_manual_frontier_ui test_cli test_execution_ui test_architect_slice_ui test_current_evidence_projection test_acceptance_coverage`) ran 213 tests with 2 failures — exactly the two documented pre-existing `test_acceptance_coverage` cases (`test_stale_worktree_digest_result_does_not_prove_current_code`, `test_untracked_new_file_creation_invalidates_earlier_proof`), which assert on `_finalize_report`'s return value in `workflow/vertical_slice.py`, a module this change does not touch. A seven-module UI/verification/evaluation run (`test_ui test_ui_copy_and_accessibility test_intake_ui test_discovery_ui test_verification test_verification_contract test_evaluation`) passed 173/173. `python -m compileall -q src tests` passed. `git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check` passed; plain `git diff --check` remains noisy in this checkout from its mixed CRLF/LF working-tree state, as previously documented. The full suite has since been run to completion for this change; see the "Full suite, ADRs 0072-0074" row above. **No live local or hosted rerun was performed**; this is fake-provider and unit evidence only, and it makes no claim about the Crisis Atlas product defects, which belong to remediation slices B-D. |
| ADR 0073 status | Crisis Atlas remediation slice B. `verify-web-product` treated every `fetch`/`XMLHttpRequest`/`WebSocket`/`EventSource` as forbidden under `--forbid-external-resources` without ever looking at the URL, collapsing "no third-party dependency" and "no request at all" into one rule. The Crisis Atlas plan required the dashboard to call a local HTTP API *and* configured that flag, so the only action satisfying every check was deleting the integration — which is what the delivered `app.js` did (in-memory sample data, `Offline Mode`, incidents vanishing on reload). Adds `classify_request_target` as the single definition of "external" for both script requests and document assets: relative and root-relative targets are same-origin; cross-origin, protocol-relative, WebSocket, absolute-loopback (`http://localhost:8000/x` is still a hard-coded origin), and other schemes are not; `${base}/x` is unproven while `/x/${id}` is not. `--forbid-external-resources` now means only "no third-party origin"; the pre-0073 blanket meaning is preserved under the separately named `--forbid-runtime-network-apis`, which is the migration path. Compliant requests are reported as INFO findings so an owner can see the product does call its backend. Adds `WebCheckEvidence` (element references, CSS selectors, local assets, same-origin and cross-origin API references, unproven references, end-to-end behavior measured) plus a `ceiling_statement()`; the CLI prints both on every run, and a run that cross-checked nothing raises a `negligible_evidence` warning instead of looking identical to one that verified a whole UI. Adds `ContractFindingCode.CRITERION_ASKS_FOR_BEHAVIOR`, a WARNING raised from an explicit word table when an owner's criterion text describes persistence, reload/restart survival, API round trips, or interaction; it never changes `evidence_level` and will produce false positives by design. **Observed results, 2026-07-29:** `python -m unittest tests.test_verification_contract` passed 71/71 (up from 25). A nine-module touched run (`test_verification_contract test_verification test_doctor test_cli test_local_power_session test_agent_loop test_execution_authorization test_ui test_execution_ui`) passed 319/319 with 1 expected skip. Exactly one pre-existing test changed behaviour and was rewritten: `test_a_network_call_is_an_error_for_a_dependency_free_product` asserted that `fetch('/data')` errors under `--forbid-external-resources`, which encoded the defect; it is replaced by one test asserting the opposite and one preserving the old behaviour under `--forbid-runtime-network-apis`. The two pre-existing `test_acceptance_coverage` failures are explicitly out of scope and were not run as part of this change. The CLI was run by hand against two constructed products: a dashboard using `fetch('/incidents')` now passes `--forbid-external-resources` (exit 0) and fails `--forbid-runtime-network-apis` (exit 1); a data-attribute-driven product passes with `is_negligible` true, prints the zero-evidence ceiling, and fails under `--treat-warnings-as-errors` (exit 1). `python -m compileall -q src tests` passed. The full suite has since been run to completion; see the "Full suite, ADRs 0072-0074" row above. No live local or hosted rerun was performed; this is deterministic and hand-run CLI evidence only. It removes the contradiction that made deleting the integration rational; it does not repair the Crisis Atlas product, add the integrated final gate (slice C), or re-run the regression scenario. |
| ADR 0074 status | Crisis Atlas remediation slice C. Every slice was verified in isolation and nothing ever executed against the combined result: `prepare_plan_delivery` checked task states and commit ancestry, then shipped. Crisis Atlas therefore delivered four green slices and a backend the UI never called — a defect no per-slice check could reach, because no individual slice was wrong. Adds `apoapsis.architect.final_verification`: resolves the integrated commit/branch/worktree, captures the worktree fingerprint *before* running (a check may leave byproducts, ADR 0063), executes only the plan's own `whole_project_verification_commands` against a narrowed `VerificationConfig`, computes whole-plan acceptance coverage from the immutable result, and persists `final-project-verification.json` whether it passes or not. `prepare_plan_delivery` gates on it before the archive is written and before `mark_executed`; a `failed`/`not_configured`/`commands_unavailable` outcome leaves the plan APPROVED with no ZIP and no `delivery.json`. Records are bound to commit + fingerprint; a stale or malformed one is re-run rather than reused, never substituted. `required=True` is forced for the final run because the configured set runs for every slice, so a real integration check must be `required=false` for ordinary slice execution and would otherwise never aggregate to FAILED at delivery. `PlanDelivery` moves to schema 1.1 with a separate `final_project_verification` field; the frontier handoff and ZIP usage guide keep per-slice history and integrated verification structurally apart and name the criteria the integrated run did not prove. Plan validation gains five structured ERROR findings (`MISSING_WHOLE_PROJECT_VERIFICATION`, `UNASSIGNED_INTEGRATION_CONTRACT`, `UNASSIGNED_DELIVERY_ARTIFACT`, `UNVERIFIED_END_TO_END_SCENARIO`, `INTEGRATION_FORBIDDEN_BY_VERIFICATION`) plus `IntegrationContract.runtime_boundary`, so the "integration required but runtime networking forbidden" contradiction is a lookup of Apoapsis's own documented flags rather than an inference from contract prose; ADR 0073's keyword criterion warning stays advisory and gates nothing. **Breaking:** `prepare_plan_delivery` now requires `verification_config`, and a plan with no whole-project verification command is invalid and cannot be approved — one approved before this change must be revised and re-approved, because there is no evidence to substitute. **Observed results, 2026-07-29:** `python -m unittest tests.test_architect_validation` passed 41/41 (up from 20); `tests.test_architect_slice` passed 41/41 in 155s (up from 32, including 9 new `FinalIntegratedVerificationTests`); `tests.test_planning_evaluation` passed 10/10 after its `_v2_plan` fixture gained a whole-project command; a twelve-module touched run (`test_architect_slice test_architect_validation test_architect_slice_ui test_architect_store test_architect_cli test_ui test_cli test_schemas test_planning_evaluation test_verification test_verification_contract test_current_evidence_projection`) passed **287/287** in 247s. `python -m compileall -q src tests` passed and the scoped `git diff --check` passed. The full suite has since been run to completion; see the "Full suite, ADRs 0072-0074" row above. Details in `docs/evaluation/adr-0074-final-integrated-verification-2026-07-29.md`; **no live local or hosted rerun was performed**, and this does not repair the Crisis Atlas product, add the operability contract (slice D), or re-run the regression scenario. |
| Version/state | Committed `1.0` through ADR 0034. ADR 0035 guided workflows/planning research and ADR 0036 hardening/compaction are working-tree changes. Check `git status` for exact local state. |
| Branch | `main` |
| Preserved tag | `substrate-v0.1` at `4c2e735`; never move or delete it. |
| Live local coding | Qwen3-Coder-Next Q4 has completed controlled tasks, but reliability is not established. A six-run planning comparison reached 0/6; two later single-slice probes both completed. The contrast remains unexplained. |
| Live hosted coding | Not run. Hosted paths have deterministic fake-provider coverage only. |
| Live browser | Task intake/execution, review, plans/slices, discovery/manual frontier, launcher, and guided-workflow surfaces have each been exercised in the real loopback UI. See ADRs 0023-0035 and dated evaluation records. |
| Live Docker | The pinned `python:3.12-slim` sandbox success path and isolation checks passed on 2026-07-20. See `docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`. |

Never turn fake-provider coverage into a live-provider claim. Never describe
working-tree changes as a committed release.

## Product thesis

Apoapsis is a local-first, auditable control plane for verified AI coding. It
makes bounded local or hosted models useful by giving them typed opportunities
to inspect, propose edits, and request configured checks. Apoapsis—not a
model—owns context selection, action execution, patch validation, retry ceilings,
workflow transitions, verification, completion, and audit records.

The intended primary flow is:

```text
request -> structured specification -> user approval -> deterministic context
-> deterministic route -> bounded coding proposals -> patch policy
-> configured verification -> COMPLETE or HUMAN_REVIEW_REQUIRED
```

Larger work may first pass through discovery, optional planning research, a
frontier/manual architecture plan, plan validation and approval, and then one
explicitly selected slice at a time.

## Non-negotiable authority boundary

| Capability | Authority |
| --- | --- |
| Interpret a request or propose a plan | Model proposes typed data; schemas and user approval govern acceptance. |
| Preserve hard constraints | Deterministic validation retains exact user source text. |
| Select repository context | Context compiler. |
| Choose local/frontier/human route | Deterministic routing from risk and configuration. |
| Read/search/edit/request checks | Model requests one typed action; Apoapsis validates and performs it. |
| Access external research | Restricted source adapters fetch allowlisted sources; models receive sanitized evidence only. |
| Apply a patch | Unified-diff parser, policy validator, and Git applier. |
| Decide pass/fail | Verification runner. |
| Retry, escalate, or complete | Fixed controller rules and configured ceilings. |
| Record audit history | Append-only harness stores. |

Models never receive direct shell, filesystem, Git, network, credential,
workflow-transition, retry-limit, verification, completion, or audit authority.
Changing this boundary requires an explicit ADR before implementation.

### The boundary is being split in two (ADR 0077, decided, not yet implemented)

The rule above conflates two different things: *ephemeral capability* inside a
disposable environment, and *durable authority* over the owner's repository,
network, credentials, workflow, evidence, and delivery. The Crisis Atlas
unrestricted control showed that denying the first bought no safety the
container boundary does not already provide, and cost most of the model's
engineering ability — the same Qwen used eight times *fewer* input tokens
through the typed loop and produced a materially worse product.

ADR 0077 keeps the second denial exactly as written above and permits the first
inside a disposable workcell:

> Qwen may act like a normal coding agent inside a disposable workcell.
> Apoapsis alone decides whether any resulting delta is valid, verified,
> checkpointed, promoted, or delivered.

This does **not** authorize a model shell on the Windows host, network or
credential access by prompt instruction, model-selected verification or
acceptance policy, or model-owned completion, Git promotion, plan approval, or
delivery.

The `apoapsis.workcell` package implements part of that boundary. A live
native-ext4 workcell has passed all 22 containment probes and the complete
relay path has passed through a one-token local-model generation. The real
Qwen CLI and the nine-check conformance suite have **not** run: the repository
contains conformance classifiers but no driver that produces their live
observations. Until the capability spike returns `CAPABILITY_PRESERVED` with both
`contained` and `conformant` true, the typed Local Power path below remains the
only local execution mode, and the Capability Sandbox must not be described as
working execution. See
`docs/evaluation/slice-2-live-gate-2026-07-30.md`.

| Layer | Module | Enforced by |
| --- | --- | --- |
| Pinned identity | `workcell/pins.py` | Every field required; one manifest digest per experiment |
| Container lifecycle | `workcell/controller.py` | One persistent container per session; run-id label ownership; fail-closed teardown |
| Containment | `workcell/containment.py` | 22 probes over host filesystem, credentials, network, container control, controller authority, privilege, and ceilings |
| Provider conformance | `workcell/conformance.py` | Nine checks; a malformed tool envelope is an adapter defect until they pass |
| Native loop | `workcell/events.py` | One-way `stream-json` adapter; no second model-action scheduler |
| Capability spike | `workcell/spike.py` | Observed behaviour vs the frozen control; no acceptance repair |

The container runs `--network none`. The model endpoint is reached only through
a Unix domain socket the controller creates, owns, meters, and deletes, exposed
inside the namespace on a loopback port by a read-only forwarder mounted
outside `/workspace`. There is no default route and no DNS, so egress is a
boundary rather than a policy.

| Egress layer | Module | Property |
| --- | --- | --- |
| Forwarding policy | `workcell/relay_policy.py` | Not a proxy: the upstream comes from configuration, never from the client. `CONNECT`, absolute-form URIs, and cross-origin redirects are refused; routes are a constant config can narrow but never widen. |
| Relay server | `workcell/relay.py` | Unix socket, streaming forwarded not buffered, cancellation propagated upstream, request/response ceilings, concurrency and session budgets, refusals always recorded. |
| Forwarder | `workcell/forwarder.py` | Read-only, outside the worktree, hashed into the manifest, and deliberately policy-free. |
| Portability | `workcell/platform_support.py` | Refuses a Windows-host or DrvFs socket path up front: Docker Desktop cannot carry a socket inode from a Windows filesystem into the Linux VM. |
| Readiness | `workcell/relay_preflight.py` | One-token request through the whole path, cross-checked against the relay's own counter — steps passing with zero relay traffic is a containment failure, not readiness. |

`Host` and `X-Forwarded-*` are stripped rather than refused: `Host` is
mandatory in HTTP/1.1, and the safety property is that the relay never consults
it, not that the client omitted it.

### Local Power Sandbox (ADR 0059, experimental, disabled by default)

`[execution.local_power]` adds a second, opt-in execution path for *local*
models only. It widens the action protocol — whole-file `write_file`,
`delete_file`, an atomic multi-file `propose_change_set` (ADR 0071), and a
mediated `run_shell` instead of hand-authored unified diffs — inside the
disposable per-task worktree. It does not move the boundary above:

| Capability | Under the sandbox mode |
| --- | --- |
| Edit project files | Model sends whole-file content; the harness computes the diff. |
| Change several files at once | Proposal only (ADR 0071). `propose_change_set` is validated in full before a byte is written and applies completely or not at all; there is no patch operation and no partial mutation. |
| Reach Apoapsis internals, `.git`, `.env*`, keys, home, system paths | Refused by `SandboxGuard` on every read, write, delete, and shell argument. |
| Run a command | Only allowlisted program prefixes, never through a shell, always with the sandbox as cwd, a scrubbed secret-free environment, a hard timeout, and capped audited output. |
| Network | Denied by default (`allow_network = false`). |
| Mutate workflow state or audit | Never. |
| Decide completion | Never. `finish` ends the model's turns; configured verification decides the outcome. |
| Decide *when to stop* | Never (ADR 0069). The harness ends the session itself as soon as every required command has passed for the current worktree fingerprint. An identical model-requested re-run at an unchanged state is refused, and the recorded pass is reused at finalization rather than re-run. |
| End the session having ignored a failing check | Bounded (ADR 0070). `finish` is refused — at most twice — while a required command has no result for the current state and nothing has been edited since the last check. Any edit, or actually running that command, satisfies the gate; succeeding is not required. |

Every failing check is normalized into `<verification:NAME>` evidence and an
audit artifact, and a resumed session is seeded with the prior stage's
unresolved failure, so a repair continuation can see what it is repairing
(ADR 0070). The prompt states per-command verification state as four distinct
values — passing for the current code, failed, passed before an edit
invalidated it, never run — because collapsing the middle two into "passed"
is how a stale result comes to look like proof.

A slice is a coherent, independently verifiable increment, not a file (ADR
0071). One `propose_change_set` may create, replace, and delete several files
in one turn; the harness validates every operation against the same
`SandboxGuard`, reports every problem at once, applies all of it or none of
it, then runs the required checks itself. Ceilings are
`min(max_change_set_files, max_changed_files)` per proposal plus the existing
session changed-line budget, whose violation rolls the whole set back
byte-for-byte. Every proposal — applied or refused — is recorded in
`local-power-change-set-NNN.json` and `LocalPowerReviewPackage.change_sets`
with the worktree digest the harness observed beside the one the model
claimed. Setting `atomic_change_sets = false` removes the action from the
prompt and the grammar, which is what makes the one-action protocol a real
comparison arm rather than differently-worded advice.

The strict one-action loop in `BoundedAgentSession` remains the default and is
unchanged. See `docs/adr/0059-local-power-sandbox-execution-mode.md`.
The UI exposes this as a two-step **Turn on Local Power** / **Turn off** control
on Models & environment (ADR 0061). The browser does not write arbitrary TOML:
`ApoapsisUIService.set_local_power_enabled()` edits only the known execution
fields, switches `mode` to `agent`, moves an incompatible `frontier_only` route
back to `auto` when enabling, reloads the full Pydantic config, and restores the
previous bytes if validation fails.

## Current architecture

### Entry points and configuration

- `src/apoapsis/cli/app.py` owns the CLI, default project configuration, and
  command dispatch.
- `src/apoapsis/config.py` contains strict Pydantic configuration. Unknown keys
  fail closed.
- Fresh projects default to baseline completion: required verification remains
  mandatory, while strict per-criterion acceptance mapping is opt-in. Patch
  changed-path accounting expands untracked directories to individual files,
  and verification/acceptance Human Review stops can return to a bounded local
  repair continuation (ADR 0042).
- Plan validation and failed-verification repair are UI-first actions: **Verify
  plan** persists the deterministic validation result, and **Repair and verify**
  submits the existing bounded local review continuation (ADR 0043).
- A completed continuation that returns to Human Review renders as **Repair
  incomplete**; freshest failed verification keeps the repair action available.
  Test-authoring guidance requires concrete mock interfaces and isolated
  filesystem effects plus ignore rules for credential/token files; identical
  replacements are rejected clearly, and changed paths enumerate untracked files
  individually. Terminal repair polling opens a
  completed task's report instead of refetching a now-invalid review case (ADR
  0044).
- Turn-budget exhaustion triggers one harness-owned final full verification only
  when current edits are newer than the recorded command results and verification
  budget remains. Pass/fail, acceptance, completion, and audit authority remain
  entirely deterministic (ADR 0045).
- Plan-slice tasks retain the complete approved work brief, interfaces,
  exclusions, assumptions, stop conditions, and advisory paths/symbols as
  traceable context. Older slice repairs recover that context from their exact
  approved package. Repeated unchanged diff/file observations are rejected, and
  three consecutive violations stop early as no progress (ADR 0046).
- Every Human Review case with an eligible local continuation presents the same
  **Repair and verify** action, including budget-exhausted implementation stops;
  the underlying service and authorization checks are unchanged (ADR 0046).
- A deterministic routing stop that occurred before any worktree or agent session
  offers **Run locally**. Explicit confirmation creates a fresh normal execution
  with an operation-scoped `local_only` route override; project configuration is
  unchanged, and execution authorization, isolation, patch policy, verification,
  reporting, and audit remain mandatory (ADR 0047).
- AUTO high-risk execution uses a maximum finite local profile first and
  escalates to frontier when configured. Critical risk still requires an explicit
  choice; routing review offers fresh local or frontier execution as available.
  Authorization packages record the effective profile, and local continuations
  retain it (ADR 0048).
- Manual frontier repair packages include cloud-safe repository excerpts, full
  verification evidence, prior agent sessions, and the exact approved slice
  package when present (ADR 0048).
- `START_APOAPSIS.cmd` is the primary Windows entry point: it accepts or prompts
  for one initialized Git project, starts configured loopback local coding
  service targets (Ollama or OpenAI-compatible `llama-server`), and opens the
  UI for that project. `OPEN_APOAPSIS.cmd` remains a UI-only fallback for an
  already-running local model service. `STOP_APOAPSIS.cmd` releases configured
  Ollama model memory and leaves shared services running.
- `.apoapsis/` is runtime state and must be Git-ignored. Initialization never
  installs software, downloads models, or creates a repository.

### Specification and workflow persistence

- `specification/` turns natural language into a typed proposal and enforces
  exact hard-constraint provenance.
- `workflow/engine.py` is the SQLite task/event state machine.
- `workflow/vertical_slice.py` is the primary execution controller.
- Browser and CLI paths call the same services; browser code does not infer
  authoritative state or construct provider/shell actions.

Core task states are persisted and optimistic-versioned. Only valid state
transitions may append events. A model response is never itself a transition.

### Durable operations

Long-running intake, execution, review, discovery, and planning-research work is
represented by durable SQLite operation records and background workers.
Operations use leases and heartbeats, re-read authoritative state at execution
time, and fail terminally on stale versions, changed repository state, lost
leases, or authorization drift.

Execution authorization captures task/version, repository HEAD and fingerprint,
effective config, model identities/roles, budgets, policy, verification catalog,
and hashes before provider construction or worktree mutation.

### Repository context and isolation

- The context compiler deterministically selects bounded files/excerpts and
  records attribution plus measurements.
- Secret-like paths and `.apoapsis/**` are excluded from cloud transmission.
- Agent inspection is read-only and bounded. Repository search has a pure-Python
  fallback when ripgrep cannot launch.
- Execution requires a clean parent repository, then creates an isolated Git
  worktree. Apoapsis never stashes, resets, commits, merges, or discards user
  work automatically.
- Session-patched compile-time excerpts are labeled stale in transmitted context
  when a fresh worktree version also exists.

### Bounded coding protocol

`agent/` accepts exactly one typed model action per turn:

- `search_repository`
- `read_file`
- `inspect_diff`
- `propose_patch`
- `replace_text`
- `create_file`
- `run_check`
- `submit_for_verification`
- `request_escalation`

The loop has separate turn, patch, verification, search/read, observation, and
transmission ceilings. Defaults are 20 local turns with 14 patch attempts and
14 frontier turns with 9 patch attempts (ADR 0049); the slice
`max_criteria_per_slice` ceiling (`[architect.ceilings]` in
`.apoapsis/config.toml`) is 20, paired with a `max_work_brief_chars` of 3,500
so the larger work brief stays consistent with the larger criterion budget.
Raising a ceiling changes configuration, not model authority.

Patch attempts are incremental against the current worktree. Dependency, test,
verification-config, binary, secret, metadata, and out-of-root changes are
governed by `PatchPolicyConfig`. New configurations allow non-deleting test-file
changes and dependency-manifest edits by default so from-scratch work can create
its verification suite and declare required libraries; owners may explicitly
disable either. Test deletion and verification-config changes remain forbidden.
Policy decisions and
rebased applied patches are audited.

For llama.cpp-served local GGUF models that echo chat-template tool-call
fragments, `parse_agent_action` performs one narrow recovery for `create_file`:
known closing tool-call markers are trimmed from the literal content and a
cross-action `command_name` field is discarded before the usual strict
discriminated-union validation. Unknown authority fields and every non-`create_file`
action remain fail-closed (ADR 0058).

For a single-hunk text new-file diff from `/dev/null`, the parser
deterministically restores missing outer addition markers and recomputes the
new-side hunk count before policy validation. The original and normalized forms
are audited. Existing-file edits and other diff shapes remain strict. Agent
prompts receive the effective dependency/test edit flags so instructions agree
with enforced policy.

Structured edit actions never make the model author diff syntax, and Apoapsis
will not synthesize a patch that Git's own whitespace policy must reject:
`replace_text` and `create_file` normalize end-of-file blank content the edit
introduced, and an edit that reduces to only added EOF blank content is refused
outright (ADR 0063). Files whose end-of-file shape was already unusual are left
alone. The no-progress guard for `git apply` whitespace rejections applies to
every edit action, not only `propose_patch`, and its next-action guidance names
the failure without asserting which action the model took. Three same-class
failures stop the session rather than draining the patch and turn budgets.

Reviewer-facing changed files answer one question -- what did the model change?
`apoapsis.repository.changed_paths` classifies a changed-path listing into
reviewable work and generated byproducts (`__pycache__/**`, `*.py[cod]`,
`.pytest_cache/**`, coverage/build caches). Classification is name-based and
deliberately independent of `.gitignore`, so output is correct for repositories
Apoapsis did not initialize; a path already tracked in Git is always reviewable
regardless of its name. `AgentSessionResult` and `LocalPowerReviewPackage`
report `generated_byproducts` separately, and
`RepositoryInspector.changed_paths()` remains the raw audit view.

Known-impossible verification contracts fail before model spend. Currently this
includes required Python unittest discovery from a missing start directory when
test edits are forbidden. CLI and browser submissions surface the actionable
preflight error without creating an execution operation; the browser handler
returns a structured conflict response rather than dropping the connection.
Ordinary failing tests still become repair evidence; Apoapsis does not guess
that they are impossible.

When test edits are allowed, a missing required unittest discovery directory is
instead a live implementation obligation transmitted on every agent turn. After
the matching real verification failure, escalation for that missing scaffold is
rejected and audited so the model must propose meaningful tests within its existing
budgets. Other escalation paths and all verification authority remain unchanged.

### Verification and completion

Only configured command names may run. Commands execute through the configured
host or Docker backend with bounded time/output and a restricted environment.
Failure normalization records root errors and useful locations as repair
evidence. Identical verification on an unchanged worktree is rejected.

Apoapsis-owned verification runs with `PYTHONDONTWRITEBYTECODE=1` beneath the
allowlisted host environment, so a Python check does not write `__pycache__`
into the worktree it is measuring (ADR 0063). An explicitly configured command
can override it through the existing per-command `environment` field and no
other path.

Python dependency bootstrap is harness-selected from `requirements*.txt` or
`pyproject.toml` and runs before configured checks by default. Pip installs into a
task-scoped target, which is added to `PYTHONPATH`; package build/install scripts
are explicitly allowed. The bounded installer result is a required audited
verification command. Models still cannot submit raw install commands. Host mode
therefore executes model-influenced package code without isolation; prefer Docker
when that risk is unacceptable.

Under `baseline` (the initialized-project default), all required checks passing
is enough for completion. Under `strict`, every active acceptance criterion
must additionally map to an explicitly owner-designated acceptance command and
be deterministically proven on the current worktree fingerprint. Acceptance is
never auto-designated. A model saying “done” has no effect.

`apoapsis.verification.contract` grades the configured contract's evidence
structure — `none`, `development_only`, `acceptance_designated`,
`criterion_mapped` — from required/acceptance designation and criterion
mapping only (ADR 0069). It never inspects argv to guess whether a command
exercises the product, because nothing in argv can answer that. The assessment
appears in Doctor, in the hashed `ExecutionAuthorizationPackage`, in
`FinalTaskReport.verification_contract`, in the Local Power review package and
its own audit artifact, and in the UI beside both the start confirmation and
the outcome. It reports; it never blocks, and baseline semantics are unchanged.

`apoapsis verify-web-product` is an owner-configurable check, not a harness
default: it cross-references a dependency-free browser product's HTML, CSS, and
JavaScript against each other (unresolved element lookups, dead style rules,
duplicate ids, duplicate top-level functions, missing local assets, optional
external-resource and network prohibitions) and exits non-zero on error
findings. Behavioral in-browser verification is a defined seam with no provider
implemented; requesting it fails rather than passing.

Held-out evaluation oracles are separate from development verification and
never become repair context.

### Routing, providers, and spend

- `models.frontier` remains the specification/legacy provider.
- `models.local_coder` is the local coding role.
- `models.frontier_coder` is optional and separately authorized.
- `models.local_research` is a tool-less local synthesis/extraction role.
- Provider calls pass through instrumentation for tokens, latency, cache use,
  model/role identity, and configured cost estimates.

Hosted calls require explicit provider configuration. Evaluation lanes that can
use hosted models also require an aggregate maximum hosted-spend ceiling and
fail before any call if the pessimistic allowance exceeds it.

Fresh `apoapsis init` projects now default the coding/specification roles to
Laguna S 2.1 served by a local OpenAI-compatible `llama-server` endpoint:
`provider = "openai_compatible"`, `base_url = "http://127.0.0.1:8000/v1"`,
`model = "Laguna-S-2.1-UD-Q4_K_S"`, and
`context_window_tokens = 32768` (ADR 0060). The OpenAI-compatible adapter may
omit the `Authorization` header only for loopback endpoints (`localhost`,
`127.0.0.1`, or `::1`); non-loopback hosted endpoints still require the
configured credential environment variable before a request is sent.
`START_APOAPSIS.cmd`/`operator_lifecycle.py` can now health-check and warm such
loopback OpenAI-compatible targets. If the endpoint is down, it launches only an
explicit operator-provided `APOAPSIS_LLAMA_SERVER_COMMAND`; it never downloads,
installs, initializes a repository, or manages hosted endpoints (ADR 0062).

### Research Mode

`research/` is advisory and quarantined. The model proposes a typed plan;
Apoapsis validates allowed sources and budgets. Restricted GitHub, official-doc,
and optional Reddit adapters perform network access. Content is fetched with
domain/content/size/redirect/time limits, sanitized for prompt injection,
license-classified, provenance-bound, cached, and audited before a tool-less
local model extracts evidence or synthesizes patterns.

Candidate capacity is distributed across planned queries. One available source
may fill the fetch budget; diversity limits apply when multiple sources exist,
and, since ADR 0055, a per-research-question cap in `SourceRanker` also
prevents one broad query from consuming the entire fetch allowance meant to
be shared across every viable research question. Sources with no extracted
findings appear in `rejected-evidence.jsonl`.

ADR 0055 fixes a reproduced failure (operation
`DISCOP-796622810B804FE59E87536D`) where an empty `evidence.jsonl` always
raised the same generic "no provenance-valid research evidence remained"
error regardless of cause. `ResearchEngineError` now carries a
`ResearchFailureReason` (no source candidates; a planned source unusable for
its adapter; sources retrieved but nothing relevant extracted; findings
rejected by provenance validation; insufficient source diversity) and a
structured `detail`; the discovery operation service appends a
reason-specific recommended operator action to the persisted failure.
Every planned query is checked for feasibility before retrieval --
concretely, an `official_docs` query with no URLs and no configured search
provider, or whose URLs are all outside `allowed_domains`, is recorded in a
new `unusable-queries.jsonl` audit file and excluded rather than silently
contributing nothing; if literally no query is viable, research fails fast
with that reason instead of continuing on an unrelated adapter. When
retrieval produces sources but the first extraction pass finds nothing
relevant, the engine runs exactly one bounded, audited recovery pass over
the same retrieved sources (no new fetch, no larger budget, never a second
round) before giving up; `recovery.json` always records whether it ran.
`OfficialDocumentationSource` gained an optional
`OfficialDocumentSearchProvider` seam (`sources/search_provider.py`) for
real official-document discovery -- query proposal, then a deterministic
search provider, then domain filtering, then the existing ranker, then the
existing restricted fetcher. ADR 0056 records the owner's explicit
authorization of Tavily as the one concrete provider implemented behind
that seam (`TavilyOfficialDocumentSearchProvider`, `sources/tavily.py`;
Brave Search was the initial pick but was dropped after its free tier
turned out to require a credit card and metered billing, unlike Tavily's
no-card free tier): `search_provider = "tavily"` plus
`search_credentials_env` (default `TAVILY_API_KEY`) enables it, and
`api.tavily.com` must be added to `[research.security].allow_domains`. Any
other provider name still fails clearly (ADR 0055) rather than guessing at
Bing/Brave/Serper/etc. Direct-URL official-doc research is unchanged and
still needs no provider (`search_provider = "none"` remains the default).
The Tavily integration has deterministic fake-fetcher test coverage only --
no live call to the real Tavily API has been made or verified in this
session; no API key was available.

Research never writes project files, executes downloaded code, sees project
secrets, approves a plan, creates a coding task, or authorizes a slice. Coding
agents do not receive general internet access.

### Discovery and Architect Mode

- `discovery/` supports bounded local clarification questions, verbatim user
  answers, one typed `IdeaBrief`, explicit user approval, optional research,
  and an immutable frontier planning package.
- Harmless bullet/case/whitespace noise in a proposed constraint quote is
  resolved back to the exact characters from the user's idea/answer; paraphrase
  still fails.
- Frontier planning may use an explicitly configured API with spend controls or
  a manual ChatGPT/Claude subscription export/import. Subscription sites are
  never automated.
- `architect/` validates a typed plan, verification names, constraints,
  dependencies, paths, and ceilings before explicit approval.
- A plan never executes automatically. One approved, dependency-ready slice is
  packaged and approved at a time. Its derived task preserves the full approved
  slice execution contract rather than only the objective and inherited
  constraints. Completion does not commit or merge; the operator does that in
  normal Git before dependent slices become ready.
- After every slice is COMPLETE, **Prepare finished project** checkpoints the
  final integrated task branch, writes a tracked-source ZIP with a usage guide,
  records the plan as EXECUTED, and emits a whole-project frontier-review handoff.
  It never moves or merges the user's checked-out branch (ADR 0048).

### Finished-plan delivery

`architect/delivery.py` checkpoints the integrated tip, refuses any slice
whose current evidence is unreadable (ADR 0072), then runs
`architect/final_verification.py` — the plan's own
`whole_project_verification_commands` against the exact integrated commit,
bound to that commit and to the worktree fingerprint captured before the run
(ADR 0074). Only a passing, matching record permits delivery; anything else
leaves the plan APPROVED with no archive and no `delivery.json`, and the
refusal's reasoning is persisted at
`.apoapsis/plans/<plan-id>/final-project-verification.json`.

`PlanDelivery` (schema 1.1) keeps two evidence fields that make different
claims and must never be conflated: `verification_summary` is per-slice
history, scoped to one task each and carrying no commit or fingerprint
binding; `final_project_verification` is the integrated run. The frontier
handoff and the ZIP usage guide preserve the same separation.

Plan validation enforces the structural preconditions: a plan must name a
whole-project command, every integration contract must be assigned to a
slice, every required delivery artifact must be produced by one, every
end-to-end scenario must be proven by a whole-project command, and a
contract's declared `runtime_boundary` must not be forbidden by the flags of
a command that governs it.

ADR 0076 adds the operability half. `PlanDeliveryContract` carries
`launch_verification_command` (the name of a configured command, never a
shell string) or `launch_not_runnable_reason`, and validation requires
exactly one. `primary_documentation_path` must be set, safe, and assigned to
a slice. At delivery, `assess_delivered_operability` compares the contract to
the integrated commit's file inventory and refuses a plan whose required
artifacts are not actually shipped; the resulting `DeliveredOperability`
record on `PlanDelivery` separates "artifact present", "launch exercised by
this command", and "launch explicitly unmeasured for this reason". The ZIP
usage guide renders the plan's own structured install/launch/test text and
labels its filename heuristics as inference rather than documentation.

### Human review and manual frontier repair

Budget exhaustion, unavailable escalation, policy stops, provider failures, and
verification/acceptance gaps become deterministic Human Review cases. Review
actions are computed from persisted state. A continuation requires explicit
authorization and has additive bounded budgets recorded in its package.

A pre-agent deterministic routing stop is not a continuation. **Run locally** is
an explicit user authorization for one fresh local execution and is only eligible
while no worktree or local session exists. A failed start returns to the same
routing-review class so the operator can inspect or retry without an unknown-state
dead end.

Manual frontier repair exports one hash-bound Markdown package, imports a typed
response, requires explicit approval, applies through normal patch policy, runs
normal verification, and records subscription usage as unmeasured.

### Local operator interface

`ui/application.py` is the server-side authority boundary and
`ui/static/app.js` is a state renderer/action client. The loopback API uses a
capability token. The UI covers Home, New Task, specification approval, task
control/changes/review/report, Plans and slices, Discovery, Evaluations, and
Models & environment.

The interface must distinguish user authority, model proposals, control-plane
actions, repository evidence, and deterministic results. Missing measurements
say `Unmeasured`, never zero. Detail-route errors must clear stale prior content.

### Native desktop shell (spike only, ADR 0050)

ADR 0050 supersedes ADR 0034's native-wrapper deferral and adopts Tauri 2 as
the target desktop shell, with the existing Python `ui/server.py` application
unchanged underneath it. Only Phase 1 (a disposable technical spike) is built
so far: `spikes/native-shell-tauri/` contains a `backend_entry.py` child
process entry point (spawns the existing, unmodified
`apoapsis.ui.server.create_ui_server`) and a Tauri 2 host (`src-tauri/`)
written to spawn it, wait for a deterministic readiness line, show a
plain-language error instead of a broken window on failure, and terminate
only its own owned child on close. `tests/test_native_shell_spike.py`
deterministically proves the backend child-process lifecycle and
capability-token behavior that host relies on. A later pass installed a real
Rust 1.91 toolchain and confirmed the Tauri 2 dependency graph in
`Cargo.toml` actually resolves/downloads and partially compiles (through
glib/gio bindings) before hitting a Linux-only GTK3 system-library gap
unrelated to the actual Windows/WebView2 target; `src/main.rs` itself has
still never been type-checked against the real `tauri` API, and no native
window has been opened. See ADR 0050 for the exact evidence boundary.

ADR 0051 implements Phases 2-3's Python service layer (no native/Rust or
HTTP wiring yet): `src/apoapsis/desktop/` contains `ProjectRegistryStore`
(a new application-owned SQLite "recent projects" store, deliberately
separate from any one project's `.apoapsis/`), `ProjectCapabilitySessions`
(in-memory, opaque, window/project-scoped capability ids -- never a raw
path), `DesktopProjectService` (`validate_project`/`select_project`
/`initialize_project`/`list_recent_projects`/`forget_recent_project`, the
last only ever calling the existing unmodified `apoapsis.cli.app._init()`,
never automatically), and `DesktopImportService`
(`preview_import`/`approve_import`/`execute_import`: staged, hash-verified,
previewed copying that hard-excludes `.git`/`.apoapsis`/`.sol`,
dependency/build/virtualenv directories, and secret-like filenames by
default; never follows symlinks; rejects traversal/absolute/reserved-name
destinations; requires explicit confirmation for replacements with an
automatic backup; and writes a durable JSON audit manifest under the
project's own `.apoapsis/import-manifests/`). `tests/test_desktop_registry.py`
and `tests/test_desktop_import.py` cover this deterministically; neither
has been executed in the authoring session (Python 3.10 sandbox, see
Snapshot) -- run them before treating Phase 2/3 as verified.

ADR 0052 adds Phase 4 (`DesktopReferenceService`: `attach_reference_project`
/`select_reference_evidence`/`list_reference_evidence`/
`detach_reference_project` -- read-only, one-file-at-a-time evidence
selection recording exact source project/commit/hash into an append-only
`.apoapsis/reference-evidence/<id>/evidence.jsonl` ledger, reusing ADR
0051's containment/exclusion checks) and Phase 5 (`DesktopHomeService
.home_summary()`: project identity, Git state, init state, verification
readiness via the existing `ApoapsisUIService.doctor()`, recent projects,
and a deterministic available-actions list; plus a real but unbuilt
`tauri::menu` File/View/Help structure in the ADR 0050 spike, whose
`on_menu_event` handler is an intentional stub pending a second, privileged
local-IPC channel on the same backend process -- a fresh-subprocess-per-
click design was considered and rejected because it cannot honor
`ProjectCapabilitySessions`' deliberately in-memory, restart-invalidated
lifetime). `tests/test_desktop_reference.py` and `tests/test_desktop_home.py`
cover this deterministically; like ADR 0051's tests, neither has actually
executed successfully in this sandbox (Python 3.10 lacks `tomllib`; a
separately obtained Python 3.11.0rc1 had no working `pip`) -- run them
before treating Phase 4/5 as verified.

ADR 0053 builds Phase 6's actual local IPC channel: `src/apoapsis/desktop
/ipc_server.py`'s `DesktopIPCHTTPServer` is a second `ThreadingHTTPServer`
in the *same* Python process as the browser-facing UI server (started by
`backend_entry.py` alongside it, in a background thread, when
`--desktop-token` is supplied), on its own OS-assigned loopback port,
guarded by its own capability token the browser-facing webview never
receives. It exposes exactly the fourteen typed operations Phase 6 named
as `POST /desktop/<operation>` routes, backed by `DesktopServices`
(`src/apoapsis/desktop/services.py`, bundling all four Phase 2-5 services
behind one project registry), with the same most-specific-first
error-to-HTTP-status discipline `ui/server.py` already uses. The disposable
Tauri spike (`spikes/native-shell-tauri/src-tauri/`) now generates a
second token, waits for both servers' readiness lines, and wires three
menu handlers (`open_recent`/`close_project`/`environment_diagnostics`) to
real HTTP calls over this channel; the four handlers needing a native
picker (`open_project`/`import_files`/`import_folder`/
`attach_reference_project`) remain documented stubs -- `tauri-plugin-dialog`
was not added. `tests/test_desktop_ipc_server.py` exercises the channel
over real loopback HTTP (token auth, routing, a full import round trip,
reference-evidence capture, session close) but has not actually executed
in this sandbox, same Python-version reason as ADR 0051/0052's tests.

ADR 0054 wires the spike's remaining four menu handlers
(`open_project`/`import_files`/`import_folder`/`attach_reference_project`)
to a real native picker (`tauri-plugin-dialog`, added to `Cargo.toml`),
completing every File-menu action except `show_project_folder`. It also
fills four Phase 7 coverage gaps found by checking ADR 0050's checklist
item by item: a backend readiness-timeout test (which caught and fixed a
real bug in `tests/test_native_shell_spike.py`'s own helper -- it blocked
with no timeout against a genuinely silent child process), an
import-atomicity test (a source file changed mid-execution aborts the
*whole* import, promoting nothing), a one-project-per-window binding test,
and a new `tests/test_desktop_authority_boundary.py` proving via static
source scan that no model-facing package or the browser-facing
`ui/server.py`/`ui/application.py`/`app.js` ever references
`apoapsis.desktop`. Junction rejection (vs. symlink rejection, which is
tested) and native-picker cancellation remain undeterminable outside real
Windows hardware -- explicitly disclosed as such, not silently skipped.
An ADR 0054 addendum also fixed a real dev-workflow bug found by reading
(not compiling) the spawn path -- `backend_entry.py` resolution only
checked a packaged `resources/` layout that does not exist yet, so a
plain `cargo run` would have failed immediately -- and wired the last
stubbed menu action, `show_project_folder` (an OS-appropriate "reveal in
file manager" call, not a desktop-IPC operation). Every File/View/Help
menu item now has a real, if still uncompiled, implementation.
Phases 2-8 (native project picker/registry, safe import workflow, reference-
project attachment, desktop UX, typed capability API, full deterministic
coverage, and real-Windows manual verification) are not built. A model gains
no new filesystem, shell, Git, or network authority from this work; only the
desktop controller may hold user-granted filesystem capability, and only
within the scope the user explicitly selects through a native dialog.
ADR 0055 fixes the reproduced Research Mode failure described above in the
Research Mode subsection: classified failure reasons/detail on
`ResearchEngineError`, pre-retrieval query-feasibility checks (a new
`unusable-queries.jsonl` audit file), a per-research-question fairness cap
in `SourceRanker` alongside the existing per-source one, exactly one
bounded/audited recovery pass when retrieval succeeds but extraction finds
nothing, and a harness-owned `OfficialDocumentSearchProvider` seam for real
official-document discovery with no concrete vendor implemented yet (the
vendor choice is explicitly deferred to the owner, with a recommendation
recorded in the ADR). Existing direct-URL official-doc research, the
existing GitHub/Reddit adapters, and the existing security/quarantine
pipeline are unchanged.

### Audit and reports

Task audit directories preserve prompts, requests/responses, telemetry, context
measurements, turn actions, policy decisions, normalized failures, patches,
verification results, research provenance, routing, authorization, continuation,
and final reports. SQLite operation/event databases are authoritative for live
state; JSON/Markdown artifacts are immutable evidence and handoff material.

`report.json` is written exactly once, at the task's first stop, and is never
updated. `reporting/current_state.py` (ADR 0072) is the single read-only
projection of a task's *current* outcome, verification, acceptance coverage,
and stop/completion reason, computed from persisted task state, the
append-only event history, and the operation artifact the deciding stage
wrote. Every surface that labels a task outcome reads it: the Report page and
task list (`ui/application.py`), review-case construction (`review/case.py`),
finished-plan delivery and the whole-project frontier handoff
(`architect/delivery.py`), plan slice status
(`architect/slice_service.py`), and `apoapsis inspect`.

When the event history names an evidence generation whose artifact is missing
or malformed, the projection reports empty results with an explicit integrity
flag and never falls back to `report.json`. `prepare_plan_delivery` gates on
that: a slice that is persistently COMPLETE but no longer evidenced raises
`SlicePackagingError`, the plan stays APPROVED, and no ZIP or `delivery.json`
is written. Delivery's per-slice section is labelled per-slice history and is
not presented as integrated-project verification.

## Operating the project

Requirements: Python 3.12+, Git, and the declared project dependencies. Ripgrep
is recommended. Ollama and Docker are optional unless selected by configuration.

Typical development checks:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
```

For a target repository:

```powershell
apoapsis init
apoapsis doctor
apoapsis ui --project-root .
```

`apoapsis init` writes an example configuration, not a universal verification
contract. Replace its verification command with a real project check before
execution. In strict mode, explicitly designate acceptance commands and map
criteria only when their pass genuinely proves the criterion.

## Evidence index

- Context profiles: `docs/evaluation/apoapsis-1.0-profile-evidence-2026-07-18.md`
- Strict live rounds: `docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`
  and `apoapsis-strict-live-evaluation-2026-07-19.md`
- Planning comparison: `docs/evaluation/apoapsis-planning-comparison-2026-07-20.md`
- D4c diagnosis/probes: `docs/evaluation/apoapsis-d4c-forensic-diagnosis-2026-07-19.md`
- Docker proof: `docs/evaluation/apoapsis-d5a-live-docker-evidence-2026-07-20.md`
- ADR 0072 current-evidence projection (deterministic only; the Crisis Atlas
  run is cited as motivating live-local input, not as evidence for the fix):
  `docs/evaluation/adr-0072-current-evidence-projection-2026-07-29.md`
- ADR 0073 request-policy split and web-check evidence (deterministic plus
  hand-run CLI transcripts against two constructed products; no live run):
  `docs/evaluation/adr-0073-request-policy-and-web-evidence-2026-07-29.md`
- ADR 0074 final integrated-project verification and plan cross-consistency
  (deterministic only, against real git worktrees; no live run):
  `docs/evaluation/adr-0074-final-integrated-verification-2026-07-29.md`
- ADR 0075/0076 planner handoff and operability contract (deterministic only;
  no live run):
  `docs/evaluation/adr-0075-0076-operability-and-planner-handoff-2026-07-29.md`
- Crisis Atlas 64K sliced Qwen plus Codex checkpoints (live local inference plus
  direct Codex inspection):
  `docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`
- Crisis Atlas unrestricted Qwen CLI control (live local inference plus
  independent host/browser verification):
  `docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`
- ADR 0077 paired scorer and frozen Crisis Atlas facts (deterministic rescore of
  the two records above; no new inference):
  `docs/evaluation/adr-0077-paired-scorer-and-frozen-arms-2026-07-30.md`
- Slice 2 Capability Sandbox workcell (deterministic implementation record):
  `docs/evaluation/slice-2-workcell-conformance-spike-2026-07-30.md`
- Slice 2A model relay and forwarder (relay exercised end to end over real Unix
  sockets against a fake upstream; **no container, no live model**), including
  the full-suite baseline of 12 pre-existing failures:
  `docs/evaluation/slice-2a-model-relay-2026-07-30.md`
- Slice 2 live gate (22/22 containment and one-token relay readiness passed;
  stopped with all nine conformance checks `NOT_RUN` because their live driver
  is absent):
  `docs/evaluation/slice-2-live-gate-2026-07-30.md`
- Slice 2B live conformance and captured pins (**live**: real container, real
  `llama-server`, real Qwen weights and CLI). All nine checks executed;
  containment 22/22; seven passed. Failed on `declared_limits_match_server` —
  the CLI declares a 1,000,000-token window against a 65,536-token server,
  which is the Crisis Atlas failure mode's root cause — and on
  `multiline_unicode_integrity`, which was diagnosed as model transcription
  rather than transport corruption and left failing rather than loosened. The
  prompt, tool-schema, and chat-template pins are now captured from the CLI's
  wire traffic and the server's `/props`. No paired task run, no acceptance
  repair, spike verdict `NOT_MEASURABLE`, **Slice 3 still blocked**:
  `docs/evaluation/slice-2b-live-conformance-and-pins-2026-07-30.md`
- Earlier local smoke records: remaining files in `docs/evaluation/`

Use these dated records for exact setups and observed results. Keep new live
claims there; the Snapshot above should contain only a short current summary.

## Known limitations and active risks

- Live local coding reliability is not established; run-to-run sensitivity in
  planned slice execution remains unexplained.
- No live hosted coding call has been made.
- The default initialized verification command is only an example and may not
  fit blank/non-Python projects; the known impossible unittest case now fails
  fast, but general project-check selection remains operator configuration.
- Research quality depends on allowed domains, source configuration, query
  quality, upstream search behavior, and available authentication. It is
  advisory, not proof.
- Real official-document web search has a secure provider seam (ADR 0055)
  and one implemented provider, Tavily (ADR 0056), but no live call has
  been made or verified against the real Tavily API in any session; treat
  it as untested until an API key is configured and a real run is recorded.
- Browser JavaScript still relies heavily on static regression tests; important
  flows need periodic real-browser checks.
- A passing verification contract is still not a working product. ADR 0069
  narrows the gap for browser products and makes the gap visible everywhere,
  but `verify-web-product` proves that a product's files agree with each
  other, not that the product behaves. The behavioral seam
  (`run_behavioral_probe`) has no provider and fails loudly rather than
  passing; no in-browser behavioral verification exists.
- ADR 0069's dead-style-rule warning suppresses any class name that appears as
  a word anywhere in a script, to avoid firing on client-rendered products
  whose DOM is built in template literals. That is a deliberate trade of
  recall for signal quality at warning level; error-level checks do not use it.
- ADR 0073's `CRITERION_ASKS_FOR_BEHAVIOR` reads owner criterion text against
  an explicit word table and will produce false positives; it is a WARNING
  that never changes `evidence_level` and never blocks. Its request-target
  classifier is lexical: a URL assembled from variables is reported as
  unproven rather than resolved, so a product whose API base is configured at
  runtime gets a warning under `--forbid-external-resources`, not a pass.
- `verify-web-product` still executes nothing. ADR 0073 makes the ceiling of
  a static run explicit and warns when a run cross-checked nothing, but a
  criterion about persistence, browser/API integration, or interaction
  behavior still needs a project-specific acceptance command that the harness
  cannot supply.
- Packaging a later plan slice checkpoints completed prior work on isolated task
  branches and records the exact inherited base commit. The user's checked-out
  branch remains untouched; incomplete slices and divergent histories fail closed.
- Native desktop packaging remains a disposable, unbuilt spike (ADR 0050,
  `spikes/native-shell-tauri/`); it has never been compiled or run on real
  Windows hardware. Live hosted evidence also remains deferred.

- ADR 0074 runs the plan's whole-project commands against the integrated
  commit before delivery, but what that proves is exactly what those
  commands prove. It is not a behavioral check, and the evidence-strength
  reporting of ADR 0069/0073 applies to it unchanged. A plan whose
  whole-project command is a static file check still delivers on static
  evidence; the difference is that the evidence is now about the combined
  result and is labelled as such.
- ADR 0074's contradiction detection only fires when a planner populates
  `IntegrationContract.runtime_boundary`. `unspecified` is the default and
  asserts nothing, so a plan that describes a browser/API integration only in
  prose is still not checked against a forbidding verification flag. ADR 0075
  makes the handoff ask for the field explicitly and stops the literal example
  from suggesting the default, which raises the odds a planner fills it in but
  cannot guarantee it. No live evidence yet says whether the prose is
  sufficient in practice.
- ADR 0076 enforces that a launch command exists and ran, or that the owner
  wrote down why it cannot, and that required artifacts are in the shipped
  tree. It does **not** read the README's content: a slice is responsible for
  it and its presence is checked, but whether it describes the shipped
  behaviour is a human judgement. Seed data, demo-only paths, and offline-mode
  fallbacks remain statically undetectable; the structural lever is
  `INTEGRATION_WITHOUT_END_TO_END_PROOF`, which forces a behavioural
  acceptance command to exist for a networked contract rather than detecting
  the smell itself.
- Nine pre-existing full-suite failures (7 failures, 2 errors), identified on
  2026-07-26 and not yet diagnosed. **Re-confirmed unchanged on 2026-07-29** by
  a full-suite run at the tree containing ADRs 0072-0074: 1118 tests, 12 skips,
  7 failures, 2 errors, matching this inventory exactly and adding nothing.
  Confirmed independent of ADR 0063 by reproducing each in a scratch
  copy with that ADR's behavior changes neutralized:
  `test_acceptance_coverage` (`test_stale_worktree_digest_result_does_not_prove_current_code`,
  `test_untracked_new_file_creation_invalidates_earlier_proof`),
  `test_desktop_import` (3), `test_desktop_reference` (1), `test_desktop_home`
  (1 error), `test_desktop_registry` (1 error), and `test_diagnostic_probe`
  (`test_summary_reports_the_read_loop_when_the_model_never_verifies`). The
  desktop modules had never been executed before (ADR 0051-0054 were authored
  without test runs), so these are first-execution defects of the same kind ADR
  0059's suite surfaced, not regressions.

See `NEXT_STEPS.md` for the prioritized actionable list only.

## Architecture decision index

ADRs 0001-0014 establish the deterministic substrate, providers, research,
bounded agent, routing, evaluation, sandbox, context, lifecycle, and UI.
ADRs 0015-0018 establish strict acceptance and proof integrity. ADRs 0019-0029
establish planning, review/resume, durable operations, authorization, slices,
comparative evaluation, and diagnostic probes. ADRs 0030-0041 establish hosted
spend, manual frontier paths, discovery, browser/launcher workflows, planning
research, hardening/compaction, default bounded test authoring, and deterministic
new-file diff reconstruction, default dependency authoring, plan-local slice
inheritance, required verification scaffolding as implementation work, and
harness-controlled Python dependency installation.
ADRs 0042-0048 add verification repair, UI-first validation/repair, truthful
repair results and test-side-effect guidance, automatic final verification, and
complete slice-contract/no-progress recovery, plus explicit fresh local execution
after a pre-agent routing review, strong risk-aware local execution, richer
frontier handoffs, and explicit finished-plan delivery.
ADR 0049 bumps the coupled `[architect.ceilings].max_criteria_per_slice`
ceiling (12 → 20, paired with `max_work_brief_chars` 2000 → 3500) and the
local+frontier coder budgets in lockstep so a 13–20 criterion slice validates
and is actually implementable inside the same one-coder-cycle scope; applies
to every future `apoapsis init`, never silently rewrites an existing
`.apoapsis/config.toml`. Follow-up fix (2026-07-25): `apoapsis init`'s
`DEFAULT_CONFIG` template already wrote the ADR 0049 numbers, but the plain
Pydantic class defaults on `ArchitectPlanCeilings` and `AgentLoopConfig` in
`src/apoapsis/config.py` had drifted back to the pre-ADR-0049 values (12
criteria/2000 chars; local 12/8/4/20/240/48000/24000; frontier 8/5/3) --
the fallback any `ApoapsisConfig()`/`ArchitectPlanCeilings()` construction
without a config.toml (library callers, and a project whose config.toml
omits these fields) silently got. Both classes now default to the ADR 0049
numbers; `tests/test_cli.py::test_bare_config_construction_defaults_match_adr_0049`
guards this drift going forward. `python -m compileall -q src tests` and
`git diff --check` pass; the full deterministic suite was not re-run in
this session beyond the focused modules noted above (`test_cli`,
`test_architect_validation`, `test_agent_loop`, `test_vertical_slice`,
`test_architect_slice`, `test_evaluation`, `test_acceptance_coverage`,
`test_execution_operations`, `test_execution_authorization`,
`test_manual_frontier(_ui)`, `test_planning_evaluation`,
`test_diagnostic_probe`, `test_architect_cli`, `test_review*`,
`test_doctor`, `test_spend_ceiling`, `test_context_compiler`,
`test_workflow`, `test_schemas`) -- all pass except pre-existing failures
unrelated to this change (confirmed by reproducing them against the old
defaults too): `test_acceptance_coverage`'s
`test_stale_worktree_digest_result_does_not_prove_current_code` and
`test_untracked_new_file_creation_invalidates_earlier_proof`;
`test_planning_evaluation`'s `test_monolithic_condition_completes_and_passes_held_out_oracle`,
`test_all_three_slices_complete_in_order_and_oracle_passes`, and
`test_integration_failure_is_detected_when_every_slice_completes_individually`;
`test_diagnostic_probe`'s `test_production_condition_prompt_never_contains_the_advisory_note`
and `test_summary_reports_the_read_loop_when_the_model_never_verifies`; a
CLI parser rejecting `--context-profile 64k`; and `test_doctor`'s Python-
version/ripgrep checks (environment-specific to the Python-3.10-plus-shim
verification environment used this session, not the repo's real
Python-3.12 target). `test_architect_slice`'s
`test_successful_slice_execution_reflected_in_status` also failed for an
unrelated pre-existing reason at the time this paragraph was first
written -- since fixed the same session, see the delivery-hardening
paragraph below.
ADR 0050 supersedes ADR 0034's native-wrapper deferral, adopts Tauri 2 as the
target desktop shell around the existing unchanged Python backend, and builds
only a disposable Phase 1 process-lifecycle/capability-token spike; Phases
2-8 (project registry, safe import, reference projects, desktop UX, typed
capability API, full coverage, real-Windows verification) remain future work.
ADR 0051 implements Phases 2-3 as a Python service layer only
(`src/apoapsis/desktop/`: project registry, capability sessions, and a
preview/approve/execute file-import workflow with hard-coded safety
exclusions); native Rust wiring, HTTP routes, and browser UI for these
services remain future work, and neither new test module has been run in
the authoring session.
ADR 0052 adds Phase 4 (read-only reference-project attachment/evidence
capture) and Phase 5 (a Home-screen data-assembly service plus a real,
unbuilt Tauri File/View/Help menu skeleton with intentionally stubbed
handlers) -- also Python-service-layer-only.
ADR 0053 builds Phase 6's privileged local-IPC channel (a second loopback
HTTP listener in the same backend process, its own token, fourteen typed
routes over `DesktopServices`) and wires three of the spike's menu
handlers to it for real.
ADR 0054 wires the remaining four native-picker-dependent menu handlers
via `tauri-plugin-dialog` and fills four Phase 7 coverage gaps (readiness
timeout, import atomicity, one-project-per-window, and static
authority-boundary regression tests). ADR 0055 fixes the misleading
research-mode failure message from operation `DISCOP-796622810B804FE59E87536D`:
classified `ResearchFailureReason`s and structured detail on
`ResearchEngineError`, pre-retrieval official-doc query-feasibility checks,
a per-research-question fairness cap in `SourceRanker`, one bounded/audited
recovery pass on total extraction failure, and a harness-owned
`OfficialDocumentSearchProvider` seam for real official-document discovery
with no concrete vendor implemented at that point (the choice was
explicitly deferred to the owner). ADR 0056 records that choice: Tavily,
authorized after Brave Search (the initial pick) turned out to require a
credit card and metered billing where Tavily has a genuinely free,
no-card tier -- `TavilyOfficialDocumentSearchProvider` is now the one
implemented provider, unverified against the real API in this session.
None of the six new/changed test
modules across ADR 0051-0054 have executed successfully in this sandbox
(Python 3.10 lacks `tomllib`; a separately obtained Python 3.11.0rc1 had no
working `pip`). Junction rejection and native-picker cancellation remain
outside what this environment can determine at all -- real Windows
hardware is required.
ADR 0057 (2026-07-25) responds to the Test Project 3 local-coding failure
audit: a new `create_file` action (`path`/`content`, no diff syntax at all
-- `RepositoryInspector.new_file_patch` builds the validated diff the same
way `replacement_patch` already does for edits) gives a local coder a way
to create a file without hand-authoring `propose_patch`'s new-file diff
header/hunk syntax, and `BoundedAgentSession`'s ADR-0046 repeat-failure
guard now also recognizes `UnifiedDiffError` (any malformed-unified-diff
parser failure, not just the specific message the audit hit) as a
repeat-failure class: one strike redirects the model toward
create_file/replace_text in the next prompt, two consecutive strikes make
the harness refuse a third `propose_patch` outright (no patch attempt
spent), and three consecutive strikes (including a refused attempt) stop
the session early instead of draining the rest of the turn/patch budget.
Verified in this session (same Python-3.10-plus-shim environment as
above) with `tests.test_agent_loop` (22/22 pass, including 5 new tests),
`tests.test_vertical_slice`/`test_architect_slice`/`test_evaluation` (69
tests, all pass -- see the delivery.py fix below),
`tests.test_manual_frontier(_ui)`/`test_patches`/`test_schemas` (62/62
pass), `compileall`, and `git diff --check` (clean, confirmed by an
independent line-ending-agnostic check since this repo mixes LF- and
CRLF-committed files and the sandbox's git autocrlf setting cannot match
both at once -- no genuine trailing whitespace/tabs in any changed line).
Not yet run: a real Ollama session, against either Qwen3-Coder-Next or
Laguna S 2.1 (`laguna-s-2.1:IQ4_XS`) -- this ADR pins harness-side
behavior with deterministic fake-provider tests first, per the owner's
explicit request; re-running the same failure scenario live against
Laguna is the natural next step, expected to show the local model now
reaching for `create_file` given the updated `ALLOWED_ACTIONS`/
`ACTION_RULES` prompt text in `src/apoapsis/models/prompts.py`, rather
than confirming it -- prompt compliance from an actual small local model
is exactly the part fake-provider tests cannot verify.

Delivery-hardening fix (2026-07-25, same session): `prepare_plan_delivery()`
(`src/apoapsis/architect/delivery.py`) raised `AttributeError` for any
real verification result. Two compounded bugs, not one: it read
`item.command_name` (should be `item.name`) directly off
`report.verification_results`, but that field is
`list[VerificationResult]` -- one aggregate verification *run* -- not
`list[VerificationCommandResult]`; the per-command `name`/`status`/
`exit_code` this summary needs actually live on each run's nested
`.commands`. Fixed to read `report.verification_results[-1].commands`
(the final run, matching the `[-1]`-latest convention already used
elsewhere for this same field in `agent/session.py` and
`evaluation/report.py`). New regression test
`tests/test_architect_slice.py::SliceApprovalAndExecutionTests::test_delivery_verification_summary_serializes_real_report_data`
exercises `prepare_plan_delivery()` against a genuine `FinalTaskReport`
produced by real slice execution (not a hand-built fixture) and asserts
the exact serialized `verification_summary` content, plus that it
round-trips unchanged through `delivery.json` via `load_plan_delivery()`
-- not just that no exception is raised. This was the only
`item.command_name`-shaped bug in the codebase (`grep -rn
"\.command_name\b" src/apoapsis/` found five other call sites, all on
`NormalizedFailure`, which genuinely has a `command_name` field).

Delivery-hardening review completion (2026-07-25, same session): the six
items left outstanding above are now closed, all in
`src/apoapsis/architect/delivery.py` and
`src/apoapsis/architect/slice_package.py`, with new coverage in
`tests/test_architect_slice.py::DeliveryHardeningTests` (9 new tests; full
module now 28/28). (1) Multi-slice inheritance:
`test_multi_slice_delivery_covers_the_whole_chain` chains two slices
(SLICE-2 depends on and inherits SLICE-1 per ADR 0024's no-automatic-merge
rule), completes both, and asserts `prepare_plan_delivery()` finds one
integrated commit, the ZIP contains cumulative content from *both* slices
(not just the last), and `verification_summary` lists both in plan order
-- this path already worked, it just had no test locking it in. (2)
Divergent branches: constructed by completing SLICE-1, letting SLICE-2
inherit and complete from it, then adding an independent commit directly
to SLICE-1's own worktree after the fact (simulating drift SLICE-2 never
saw) -- `checkpoint_completed_prior_slices()`'s existing ancestor-chain
check in `slice_package.py` already caught this and refused to guess, but
its error message didn't say *which* slices diverged; it now lists each
diverging slice with its short commit
(`slice_id@commit, slice_id@commit, ...`), and `delivery.py`'s "could not
locate the integrated final worktree" error was similarly widened to name
the plan, the task worktrees it searched, and the commit it was looking
for. Neither path could ever silently pick a wrong worktree -- the match
is exact full-hash equality -- so no behavior changed there, only
message clarity, verified by `test_divergent_completed_slice_branches_fail_delivery_clearly`
asserting both slice IDs appear in the raised `SlicePackagingError` and
that the plan stays `APPROVED` with no `delivery.json` written. (3) ZIP
exclusion: `git archive` at the checkpointed commit already can't include
`.git`, and `.apoapsis` is kept out of tracking by `apoapsis init`'s
gitignore guarantee (`cli/app.py::_ensure_apoapsis_gitignored`), but
nothing previously stopped a repository that force-added `.apoapsis` or a
credential file from shipping it anyway. `prepare_plan_delivery()` now
computes `_forbidden_delivery_paths()` against the final commit's tracked
file list (`.apoapsis/`, `.git/`, and credential-shaped names/suffixes --
`.env`, `.netrc`, `id_rsa`, `*.pem`, `*.key`, etc.) *before* writing the
archive, handoff, or touching plan state, and refuses with the exact
offending paths if any are tracked -- fail-closed, not a silent strip.
`test_forbidden_tracked_paths_block_delivery` proves this by force-adding
a `.env` and a fake `.apoapsis/tasks/...` tree into a slice worktree and
committing them; `test_zip_excludes_apoapsis_git_and_credential_state`
proves the normal (nothing force-added) case still ships a clean archive.
(4) Frontier-review handoff: `_frontier_review_markdown()` previously
embedded the *entire* plan as one raw JSON blob plus a bare file list --
technically complete (every slice's objective/work_brief/exclusions were
in there) but not actually reviewable without manually cross-referencing
`acceptance_criterion_ids`/`inherited_constraint_ids` against the plan's
top-level ID-keyed lists. Added `_slice_review_sections()`, which resolves
each slice's referenced acceptance criteria and hard constraints into
their actual verbatim text and renders a `### SLICE-n: title` block per
slice (objective, work brief, resolved acceptance criteria, resolved
constraints, exclusions, dependencies) ahead of the raw JSON dump, plus
explicit `## Original idea` / `## Architecture summary` sections and a
`## Cross-slice integration risks` prompt section instructing the
reviewer what to actually look for across slice boundaries. The raw JSON
dump stays too, for anything the resolved sections don't surface.
`test_frontier_review_handoff_contains_whole_project_context` asserts the
idea text, architecture summary, and each slice's actual objective/work-
brief prose (not just its ID) appear in the generated markdown for a
two-slice plan. (5) EXECUTED-status timing: traced the exact order
(archive `os.replace` -> handoff `audit.write_text` -> `mark_executed` ->
`delivery.json` `audit.write_json`) and found it was already fail-closed
end-to-end for the "crash before mark_executed" case (both prior writes
are atomic temp-file-then-rename, and `mark_executed` is one atomic SQL
transaction that leaves the plan `APPROVED` on any failure with no
`delivery.json` ever written -- confirmed by
`test_mark_executed_failure_leaves_plan_unexecuted_with_no_delivery_record`,
which injects a `mark_executed` failure via `patch.object`). The one real
question was "`mark_executed` succeeds, then `delivery.json`'s write
fails" -- traced this to already being self-healing by construction,
*given a retry*: `prepare_plan_delivery()`'s entry gate accepts a plan
already in `EXECUTED` status (not just `APPROVED`), and computes
`plan_version` from the already-current version rather than assuming a
bump in that case, so calling it again after such a crash regenerates the
archive/handoff/delivery.json from the same integrated commit and writes
a correct `delivery.json` without double-transitioning the plan or
double-writing the plan-events table. No code change was needed here --
this was a genuine "is this actually a gap" question, and the answer was
no, provided the operator (UI or CLI) retries a failed delivery rather
than treating a raised exception as final. `test_archive_and_handoff_exist_before_plan_is_marked_executed`
locks in the ordering with a `mark_executed` spy that asserts the archive
and handoff already exist on disk and `delivery.json` does not, at the
moment `mark_executed` is called. (6) Idempotence: already implemented
(the entry-gate `load_plan_delivery()` short-circuit returns the existing
record immediately, before any side effect, on a second call) but
untested; decided this is the correct behavior per ADR 0048 (a finished
plan has one canonical delivery, not a new archive/handoff/event per
click) over an "already delivered" error, since the existing record is
always available and identical either way.
`test_second_delivery_call_is_idempotent` proves a second call returns an
object equal to the first, does not rewrite the archive file (same mtime
and bytes), and does not invoke `mark_executed` again (patched to raise
`AssertionError` if called) or record a second `plan_delivery_prepared`
event. No new ADR was needed -- every change here is bugfixing/hardening
within ADR 0048's existing decisions (clearer errors, a defensive
exclusion list, richer handoff content, and locking in behavior that
already existed), not a new architectural decision. Verified in the same
Python-3.10-plus-shim sandbox: `tests.test_architect_slice` (28/28),
`tests.test_architect_slice_ui` (12/12), `tests.test_architect_store`
(14/14), `tests.test_architect_cli`/`test_architect_validation` (23/23),
`compileall`, and the line-ending-agnostic whitespace check (no genuine
trailing whitespace/tabs in any changed line across the three touched
files). Not run: the full `discover`-based suite (sandbox time budget),
so any interaction with modules outside architect/delivery is unverified
beyond what these targeted runs cover.

Research pipeline verification (2026-07-25, same session): ran
`tests/test_research_units.py` and `tests/test_research_integration.py`
(Python-3.10-plus-shim environment, as above). Found and fixed two real,
never-previously-executed test bugs in `TavilySearchProviderTests`
(`test_parses_tavily_response_and_never_leaks_the_credential`,
`test_results_are_still_filtered_to_the_official_docs_allowlist`): both
constructed a `FetchResponse` fixture without the required `byte_count`
field, raising a Pydantic `ValidationError` before the actual test logic
ever ran. Fixed by setting `byte_count=len(body)`, matching the existing
convention elsewhere in the same file. All 28 `test_research_units` tests
and all 6 `test_research_integration` tests now pass (34/34).
`compileall` and a line-ending-agnostic whitespace check are clean.
Live Tavily verification (owner-requested step 2 of the research
handoff) could not be performed in this sandbox for two independent
reasons, not just a missing `TAVILY_API_KEY`: (1) no key is configured
here at all, and (2) `api.tavily.com` is outright blocked by this
sandbox's network allowlist (`curl -sI https://api.tavily.com` returns
`403 Forbidden` / `X-Proxy-Error: blocked-by-allowlist`), so no live call
could succeed even with a key. As a partial substitute, I ran
`tests/test_research_live.py`'s existing GitHub live-smoke test (no
credentials required, gated behind `APOAPSIS_RUN_LIVE_GITHUB_TESTS=1`)
to see whether *any* live external research fetch works in this sandbox:
it failed with `Temporary failure in name resolution` inside the
`ResearchFetchProcess` worker subprocess -- `concurrent.futures.process`
workers don't appear to inherit this sandbox's proxy environment
variables, so DNS/network calls fail from the worker even though the
main process can reach the network directly. This is very likely a
sandbox artifact rather than a real production bug (the harness's own
process-pool isolation is intentional -- ADR 0026 -- but this specific
environment's proxy plumbing doesn't reach into it), but it means *no*
live external research source (Tavily, GitHub, or Reddit) can be
exercised end-to-end from this sandbox regardless of credentials; live
verification needs to happen in an environment with either direct network
access or proxy-aware worker subprocesses. Note also:
`tests/test_research_live.py` has no live Tavily test at all today (only
GitHub and Reddit) -- one should be added alongside whatever environment
does have real network access.
Separately, I exercised the full audit pipeline with the deterministic
fixture engine (`ResearchModeIntegrationTests._research_engine()`) and
inspected the resulting `unusable-queries.jsonl`, `candidates.jsonl`,
`retrieved-source-manifest.jsonl`, `recovery.json`, `evidence.jsonl`, and
`rejected-evidence.jsonl` on disk: all seven audit artifacts are written,
well-formed, and carry the fields ADR 0055 describes (deterministic
scores, license classification, prompt-injection flags, source locator
detail, relevance/confidence/limitations per evidence item). This
confirms the audit *plumbing* is sound, but says nothing about
real-world evidence *quality* (relevance against real queries, true
zero-finding rate, recovery usefulness, source diversity) -- those
numbers are only meaningful against live data and remain unmeasured.

ADR 0063 (2026-07-26) responds to the first end-to-end live Laguna run
(`TASK-EF33C00E5BD4`). Two harness-side defects, kept deliberately separate
because they answer different questions. (1) Reviewer-facing `files_changed`
mixed model-authored work with `__pycache__/*.pyc` written by the harness's
own verification run; `apoapsis.repository.changed_paths` now classifies a
changed-path listing into reviewable work and generated byproducts, and
`VerificationRunner` sets `PYTHONDONTWRITEBYTECODE=1` so the byproducts are
mostly not created in the first place. The classifier ignores `.gitignore` on
purpose -- the live scratch repository had no useful ignore rules, and
existing projects cannot be assumed to carry the ones `apoapsis init` writes
(it now writes Python cache entries too, as ergonomics only). Tracked paths
stay reviewable regardless of name, so a deliberately vendored artifact is
never hidden. (2) 13 patch attempts against 1 verification run: attempts 3-13
were the same `replace_text`, which Apoapsis itself turned into the same
`new blank line at EOF` patch (identical SHA-256) every time. Structured edits
now normalize introduced EOF blank content, an edit that reduces to only such
content is refused, the whitespace no-progress guard covers every edit action
rather than only `propose_patch`, and its guidance no longer claims the model
sent a `propose_patch` it never sent. Completion policy is untouched: the live
sample's `human_review_required` outcome was correct and stays correct.
Verified in this session with `python -m unittest tests.test_agent_loop
tests.test_verification tests.test_local_power_session tests.test_cli`
(88/88, 1 expected skip) and `python -m compileall -q src tests`. The full
916-test suite was also run; its 11 failures/errors are pre-existing and were
each reproduced with this ADR's behavior changes neutralized (inventory under
"Known limitations and active risks"). This is
fake-provider evidence only; the live Laguna rerun described in `NEXT_STEPS.md`
has not been performed and remains the gate on resuming reliability
measurement.

ADR 0064 (2026-07-26) closes two defects found while starting the live run.
Any unhandled exception in a UI request handler used to escape into
`socketserver`, which closes the connection without a response; the browser
reports only `Failed to fetch`, so the operator gets an unactionable dead end
on a server that is still running. `do_GET`/`do_POST` now dispatch through
`_guarded()`, which turns anything unanticipated into a readable 500 and
retains the traceback in a bounded in-memory ring. Per-route error mapping is
unchanged and still required -- reaching the last-resort handler is a defect,
and the response says so. Separately, a repository with no commits produced
Git's own `ambiguous argument 'HEAD'` message; `GitRepository.has_commits()`
and `head_commit()` (raising `RepositoryHasNoCommitsError`) now name the
problem and the fix, `snapshot()` and `ContextCompiler.compile()` use them, and
`prepare_discovery_operation` refuses up front with a 409 rather than accepting
the request and failing deep inside the worker.

ADR 0065 (2026-07-26) fixes a transport limit that contradicted configured
policy. `discovery.max_response_bytes` and `manual_frontier.max_response_bytes`
both default to 2 MB and are checked in the domain layer before parsing, but
every UI request body shared a hard-coded 64 KB cap, so a legitimate pasted
frontier plan was refused by the transport long before the real ceiling
applied. `_read_json_body` now takes a per-route `max_bytes`; the two
pasted-response routes derive theirs from configuration (`configured * 2 +
64 KB`, since the response travels JSON-escaped inside an envelope), falling
back to the schema default rather than to the control cap if configuration is
unreadable. Control routes are unchanged at 64 KB. Note for future work: this
was a wrong constant, not evidence that plans need splitting -- slices (ADR
0024) already keep the coder's input small, and the frontier model never had
trouble producing the plan in one pass.

ADR 0066 (2026-07-26) adds a literal, fully expanded response example to the
frontier planning handoff, emitted before the JSON Schema and generated from
the Pydantic models by `apoapsis.specification.skeleton.json_skeleton`. The
schema was already embedded in full, but nested objects were reachable only
through `$ref`/`$defs`, and a live frontier model invented plausible keys for
exactly the two sections (`delivery_contract`, `verification_strategy`) that
appeared nowhere in prose -- costing an entire written plan to an
`extra_forbidden` rejection. Schema strictness is deliberately unchanged:
`extra="forbid"` is what prevents a planner smuggling in status/approval
fields (ADR 0019), and accepting unknown keys would silently discard content.
If invented keys recur, the next step is a bounded schema-repair round rather
than loosening validation.

ADR 0067 (2026-07-26) applies ADR 0058's precedent -- deterministic, audited
normalization of model-interface transport noise -- to the paste transport.
`parse_pasted_json` strips a UTF-8 BOM and one surrounding Markdown code fence
before parsing, records what it removed, and on failure quotes the first 80
characters of what it actually received. Both are artifacts of how a chat
interface renders and copies JSON, not content a model chose to write, and both
sit at character 0 where they defeat parsing outright. Prose preambles and
scanning forward for the first `{` are deliberately not attempted: a wrong
guess would silently parse a fragment of what the model meant. Used by
`discovery/manual.py` and `manual_frontier/importer.py`.

ADR 0069 (2026-07-27) addresses two independent defects that live task
`TASK-33E0EB6476C4` exposed together. The Local Power loop did not treat a
passing verification as terminal, so a deterministic model re-requested the
same passing check until its turn budget ran out and finalization ran it once
more; sufficiency is now a harness-computed property of the configured contract
and the current worktree fingerprint, and the loop stops on it. Separately, the
task reached `COMPLETE` on an inert application because seven owner-written
static tests each confirmed a fragment existed and none confirmed the fragments
referred to one another. Contract strength is now graded and reported
everywhere the outcome is, and `apoapsis verify-web-product` gives owners of
browser products a check that would have caught it. Neither change blocks a
`COMPLETE` the configured contract supports: the harness reports what the
evidence is worth and leaves the judgement with the owner, because inferring
product quality from configuration is exactly the overreach that produced the
failure.

ADR 0070 (2026-07-27) closes the gap ADR 0069's own rerun exposed. The
stronger gate worked — `TASK-E01762481075` stopped safely at
`HUMAN_REVIEW_REQUIRED` instead of reporting a false `COMPLETE` — but the
repair continuation could not act, because the normalized
`web-product-integrity` failure was persisted and carried into the
continuation package and then dropped when the Local Power prompt was built.
Failure evidence now reaches the sandbox model on every turn and across a
resume, the prompt states which required commands are outstanding and whether
each result is current, and `finish` is bounded-refused when nothing has been
attempted about a check that has never been run. None of this improves the
model's implementation ability, and the ADR says so: it removes the excuse,
not the limitation.

ADR 0076 (2026-07-29) finishes the Crisis Atlas remediation. Its lesson is
narrower than it looks: the old usage guide was not lying, it was *inferring*
from filenames and then phrasing the inference as documentation. The fix is to
make the plan state the operability contract structurally, check the parts that
are checkable, and label the remaining guesses as guesses. Note what it
deliberately does not do — it never reads the README's content, and it does not
try to detect an offline-mode fallback. `INTEGRATION_WITHOUT_END_TO_END_PROOF`
forces a behavioural command to exist instead, which is the most a harness
executing only owner-approved commands can honestly do.

ADR 0075 (2026-07-29) is short and worth reading anyway: a structurally
present field whose *default value* silently disabled a gate. ADR 0066 fixed
missing keys; this fixed a key whose placeholder looked like an answer. When
adding an enum whose value changes harness behaviour, check what the handoff
skeleton renders for it.

ADR 0074 (2026-07-29) closes the gap ADR 0072 could only label. 0072 made
delivery *report* per-slice history honestly; 0074 supplies the missing
evidence by running the plan's own whole-project contract against the
integrated commit and refusing delivery without it. Its second half makes a
plan's internal contradictions machine-detectable, and the design rule there
is worth carrying forward: a gate reads structured fields
(`IntegrationContract.runtime_boundary`, command `argv` flags Apoapsis itself
defines), never prose. ADR 0073's keyword criterion warning stays advisory
for exactly that reason.

ADR 0073 (2026-07-29) is the one to read before touching verification policy
semantics. Its lesson is not about browsers: it is that a contract which
forbids the mechanism its own objective requires will be satisfied by
deleting the mechanism, and the model doing so is behaving correctly. Two
different policies had been given one name and one implementation. The fix
is a URL classifier both the request check and the asset check share, plus a
separately named strict option so nobody's existing intent is silently
reinterpreted. Its second half — a check that counts and reports its own
evidence — extends ADR 0069's principle from contract configuration to an
individual check run.

ADR 0072 (2026-07-29) is a reporting-integrity ADR, not a model-behavior one.
Crisis Atlas delivered a `delivery.json` and a whole-project frontier handoff
that both reported `human_review_required` with a failed verification for a
slice whose persisted state was `COMPLETE` and whose manual-frontier repair
had passed, because delivery read the one-time `report.json` snapshot. Five
separate surfaces were each reconstructing "is the report still current?"
from their own partial event tables. One projection now owns that question,
`report.json` is still never rewritten, and unreadable current evidence fails
closed instead of silently reverting to a superseded pass.

ADR 0071 (2026-07-27) is the first Local Power ADR whose subject is not
safety. `TASK-A0E17C03D69B` completed correctly and still produced a poor
product, because the model spent six turns rewriting one file of a three-file
application. The same model without the protocol produced a coherent product
and none of the repository-specific correctness. `propose_change_set` lets one
turn state a whole slice atomically while every existing boundary, ceiling,
and authority stays exactly where it was; the model gains expressiveness, not
authority. Whether it closes the coherence gap is unknown and deliberately
unclaimed — the three-arm Focus Orbit evaluation in the ADR has not been run.

ADR 0072 is documented in the "Audit and reports" section above and in
`docs/adr/0072-current-task-evidence-projection.md`. Anything that adds a new
terminal workflow transition must register its event type in
`reporting/current_state.py::_DECISIVE_EVENT_GENERATION`; an unregistered
decisive event deliberately fails closed rather than inheriting the original
report's outcome.

ADR 0077 (2026-07-30) supersedes the *execution boundary* of ADRs 0059 and
0071 without editing either. The unrestricted Crisis Atlas control falsified the
premise that a narrow action grammar is a free safety measure: the same model,
weights, and plan produced a materially better product through a persistent
shell in a disposable container while using eight times *more* input tokens, and
the sliced arm's cheapness had been hiding a capability regression behind a
single score.

Two things follow. First, ephemeral capability inside a disposable workcell is
separated from durable authority over the owner's repository, network,
credentials, workflow, evidence, and delivery; the second denial is unchanged.
Second, measurement is split so that trade cannot be made silently again:
`apoapsis/evaluation/paired.py` scores model proposal quality and harness
defect-detection quality separately and reports four release gates
independently, with **no** combined score field, and
`apoapsis/models/ceilings.py` classifies context and output ceiling conditions
so an interface limit is never charged to the model's reasoning.

`apoapsis/evaluation/crisis_atlas_facts.py` freezes both arms as replayable
facts. Their honest rescore is `INCOMPARABLE`: the sliced arm's seed commit was
never recorded and its output cap changed mid-run, so no win or loss can be read
from the pair. Slice 2 is permanently labelled *both* a proposal miss (a partial
service at the wrong package path, no export service, no tests) and a detection
miss (the harness applied it, saw inherited green, and said `COMPLETE`).

The workcell lifecycle and relay exist and have live containment/readiness
evidence. The real CLI conformance driver, pin-capture provenance, paired
quality run, candidate admission, and later authority layers do not. Do not
describe ADR 0077 as qualified execution.

Read the relevant ADR completely before altering its area. Preserve old ADRs as
history; supersede them with a new ADR rather than rewriting the old decision.

## Maintenance contract

For changes affecting architecture, workflow behavior, configuration, model
roles, context, patch policy, verification, audit artifacts, tests, or evidence:

1. Update this current-state map in the same change.
2. Update `README.md` for user-visible behavior.
3. Add an ADR for a new architectural decision; never rewrite accepted history.
4. Add deterministic fake-provider coverage for model-driven branches.
5. Run focused tests, the full suite, `python -m compileall -q src tests`, and
   `git diff --check`.
6. Update Snapshot only with results actually observed and label fake, live
   local, and live hosted evidence distinctly.
7. Update `NEXT_STEPS.md` only when active priority/order changes; remove done
   items instead of appending milestone essays.
8. Put detailed live observations in a dated `docs/evaluation/` file and link it.
9. Preserve uncommitted user work and the `substrate-v0.1` tag.

Before handoff, verify source, tests, README, this file, the relevant ADR, and
active priorities agree. Do not declare success from model output or from a
partial test run.

# Crisis Atlas 64K local-model plus Codex-frontier trial

Date: 2026-07-29–30  
Model: `qwen3.6-27b`, Q4_K_M, llama.cpp OpenAI-compatible endpoint  
Model context setting: 65,536 tokens  
Initial output cap: 8,192 tokens for Slice 1  
Continuation output cap: 16,384 tokens for Slices 2–4  
Continuation config SHA-256:
`FB4AEEB59DF299472156D4C45E63E4146C6BBF86534FB4F6FE846E54B528A786`

## Evidence boundary

This is live local inference plus direct Codex inspection and repair. It is not
fake-provider evidence and it is not live hosted inference.

The trial followed this owner-directed protocol:

1. Qwen attempted each slice using the normal bounded Local Power route.
2. Codex inspected the result after Qwen stopped, repaired the checkpoint when
   necessary, ran deterministic and browser checks, and committed it.
3. The next slice was seeded from that verified commit.

This differs from the original autonomous 32K and 64K arms. It measures the
quality of Qwen's per-slice contribution under a Codex-frontier recovery
protocol, not autonomous end-to-end completion. It therefore does not support
a controlled claim that 64K is better or worse than 32K.

`ContextMeasurement` remains an evidence-size estimate. The token figures below
are provider-reported actual per-call counts; neither proves that the model
needed or occupied the full 64K context window.

## Checkpoints and outcomes

| Slice | Qwen outcome | Codex inspection/repair | Verified checkpoint |
| --- | --- | --- | --- |
| 1 — domain/persistence | `HUMAN_REVIEW_REQUIRED` after 12 calls; unit and behavioral checks passed, configured launch check failed because `tests.test_launch` was absent | Added the missing configured launch wrapper; 46 tests and all configured checks passed | `f7785bfad1ec99f0c9c9fcb3d2ababa3d85b4c6b` |
| 2 — services/exports | Reported `COMPLETE` in one call | The accepted patch was only an incompatible `services/incident_service.py` skeleton and was never imported by tests. Replaced it with persisted `IncidentService`, deterministic JSON/Markdown `ExportService`, and focused tests | `4693b745cce97b223caf08749fe367e28fa28146` |
| 3 — HTTP API | Reported `COMPLETE` in one call | Found nonexistent static directory, unreachable export routes, non-serializable timeline/action responses, and crashing traversal handling. Repaired the server and added ephemeral loopback HTTP coverage | `91bd5cdace7d56f108f4e37543f9b2aa842e800a` |
| 4 — dashboard/integration | Reported `COMPLETE` after five calls and three verification runs | Browser flow worked, but README flags/check names were wrong, configured behavioral/launch checks did not prove their labels, mutation failures used alerts, and detail controls lacked labels. Repaired documentation, accessibility/feedback, and the actual behavioral and launch gates | `0d591d7bbf9eebd276df0bc6677f24d19f505f5e` |

## Provider telemetry

Totals across the four Qwen slice attempts: 19 calls, 258,632 input tokens,
55,364 output tokens, and approximately 1,467.5 seconds of provider latency.

| Slice/call | Input | Output | Latency (s) | Structured provider call |
| --- | ---: | ---: | ---: | --- |
| 1/1 | 4,674 | 5,204 | 125.97 | valid |
| 1/2 | 5,989 | 15 | 3.38 | valid |
| 1/3 | 7,540 | 16 | 3.64 | valid |
| 1/4 | 8,411 | 1,584 | 44.19 | valid |
| 1/5 | 8,969 | 20 | 5.17 | valid |
| 1/6 | 9,201 | 3,036 | 76.72 | valid |
| 1/7 | 10,830 | 15 | 5.85 | valid |
| 1/8 | 12,380 | 23 | 6.48 | valid |
| 1/9 | 13,562 | 8,192 | 197.21 | provider call succeeded; response artifact was truncated/invalid for the agent protocol |
| 1/10 | 13,669 | 2,985 | 76.05 | valid |
| 1/11 | 15,551 | 8,192 | 197.66 | provider call succeeded; response artifact was truncated/invalid for the agent protocol |
| 1/12 | 15,658 | 4,372 | 108.24 | valid |
| 2/1 | 10,835 | 1,341 | 37.88 | valid |
| 3/1 | 13,794 | 3,374 | 95.77 | valid |
| 4/1 | 17,815 | 8,213 | 223.55 | valid |
| 4/2 | 18,932 | 19 | 9.55 | valid |
| 4/3 | 22,290 | 14 | 10.06 | valid |
| 4/4 | 23,949 | 5,098 | 136.54 | valid |
| 4/5 | 24,583 | 3,651 | 103.60 | valid |

The output-cap increase materially affected the evidence: Slice 1 hit the old
8,192-token ceiling twice and produced unusable responses, while Slice 4's
first coherent four-file dashboard change used 8,213 output tokens—21 tokens
over the old cap. Conversely, the largest actual input was 24,583 tokens, so
this run did not pressure the 64K context limit. The defensible finding is that
the larger output cap helped; no context-size default change is justified.

## Final integrated evidence

At final commit `0d591d7bbf9eebd276df0bc6677f24d19f505f5e`:

- `python -m unittest discover -s tests -v`: 57/57 passed.
- `verify-web-product --forbid-external-resources
  --treat-warnings-as-errors`: passed; 41 element references, 64 CSS
  selectors, two local assets, one same-origin API reference, zero
  cross-origin or unproven references.
- `python -m unittest tests.test_behavioral_integration -v`: 8/8 passed,
  including four live ephemeral-loopback HTTP lifecycle tests.
- `python -m unittest tests.test_launch -v`: 1/1 passed and proved one process
  serves `/` and `/api/incidents`.
- `python -m compileall -q .` and `git diff --check`: passed.
- Browser inspection created an incident, filtered and selected it, changed
  status, added a timeline event and action item, and observed the persisted
  incident after reload. The repaired UI exposed accessible control names and
  visible mutation feedback.
- A disposable negative control added `localStorage` to `app.js`. The normal
  57-test unit gate failed at
  `test_dashboard_uses_only_same_origin_api_state`; the real checkpoint was
  unchanged and the disposable worktree was removed.

An inspected candidate archive was produced at
`.apoapsis-eval/slice-e-crisis-atlas-64k-codex-slice4-2026-07-29/crisis-atlas-64k-codex-trial.zip`.
It contains 29 tracked entries and no Git metadata, Apoapsis metadata, runtime
database, model log, credential/key file, or persisted incident data. SHA-256:
`D8D3EF89CAC60B0FD5BABD522B7FD9211886D42E00C1217BFAEDC1204E10252C`.

## Twelve-point regression disposition

| # | Result | Evidence/qualification |
| ---: | --- | --- |
| 1 | Pass | Approved plan distinguishes external internet from same-origin API use. |
| 2 | Pass | The bound imported plan validated; later direct slice tasks preserved its design rather than changing architecture. |
| 3 | Pass | Source, static cross-reference, and browser run all show same-origin `/api` use. |
| 4 | Pass | Browser creation produced backend state through the live HTTP server. |
| 5 | Pass | Browser reload retained state; loopback integration restarted the server against the same file and retained state. |
| 6 | Pass | Status, timeline, and action items round-tripped in browser and HTTP tests. |
| 7 | Pass | JSON and Markdown service exports are repeatably identical; both HTTP export routes passed. |
| 8 | Pass | Canonical `python server.py` process served UI and API; README now uses the real `--data-file` flag. |
| 9 | Pass, manual checkpoint protocol | All whole-project checks ran after the final slice on the exact final commit, but not through `prepare_plan_delivery`. |
| 10 | Partial | The deliberate offline-storage mutation failed a configured required command, so final verification would block. An actual plan-delivery attempt was not run. |
| 11 | Not run | Direct checkpoint seeding does not create an authoritative plan Report page or `delivery.json`; fabricating either would be misleading. |
| 12 | Partial | The candidate ZIP passed content inspection and has accurate README instructions, but it is a `git archive`, not an Apoapsis delivery ZIP. |

The product is successfully delivered as a verified candidate checkpoint, but
the twelve-point harness-delivery scenario is not complete because points
10–12 were not exercised through the plan delivery state machine.

## Conclusion

The trial demonstrates a useful division of labor: Qwen produced substantial
slice implementations, especially the broad dashboard, while Codex inspection
was necessary on every slice to close false-positive verification gaps or
product defects. The raised output cap prevented at least one near-certain
truncation. The 64K window itself was not meaningfully stressed, and the
Codex-assisted protocol is not comparable to the autonomous 32K arm. Keep the
default context setting unchanged and treat the context-size result as
inconclusive.

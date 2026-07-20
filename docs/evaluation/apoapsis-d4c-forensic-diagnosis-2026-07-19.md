# D4c: forensic diagnosis of the D4b read-loop — 2026-07-19

A read-only forensic pass over all six preserved D4b live attempts
(`docs/evaluation/apoapsis-planning-comparison-2026-07-20.md`,
`.apoapsis-eval/d4b-attempt-{1,2,3}/`, `.apoapsis-eval/planned-v2-project{,-2,-3}/`)
and ten previously preserved live Qwen3-Coder-Next Q4 sessions on the
older `download-service` (v1) fixture. No live model was run to produce
this document; every claim below is read directly from persisted
`agent-turn-*.json`, `agent-session.json`, and `call-NNN-{request,
response,telemetry}.json` audit artifacts.

## Normalized action sequences

| Run | Turns 1→edit | Edit turn | Post-edit sequence |
| --- | --- | --- | --- |
| mono-1 | read, search(**fail**), read, inspect_diff | T5 `replace_text` | T6 read(fresh) → T7-T12 read×6 (identical) |
| mono-2 | read, search(**fail**), read | T4 `replace_text` | T5 read(fresh) → T6-T10 read×5, T11 inspect_diff, T12 read |
| mono-3 | read, search(**fail**), read, inspect_diff | T5 `replace_text` | T6 read(fresh) → T7-T12 read×6 |
| plan-1 | read, inspect_diff, read, propose_patch(rejected), read | T6 `replace_text` | T7 read(fresh) → T8-T12 read×5 |
| plan-2 | read, inspect_diff | T3 `replace_text` | T4 read(fresh) → T5-T12 read×8 |
| plan-3 | read, inspect_diff, read, propose_patch(rejected), read | T6 `replace_text` | T7 read(fresh) → T8-T12 read×5 |

Every run: exactly one accepted edit, `run_check`/`submit_for_verification`
called zero times, `HUMAN_REVIEW_REQUIRED` on turn-budget exhaustion. 36
of the 72 total turns across the six sessions (50%) are the one
no-progress repeated read.

## Repeated reads were byte-identical, not merely similar

The raw model output (`call-NNN-response.json.content`, not the harness's
summary) was pulled for all six runs. From the point of no progress
onward, every response is **byte-for-byte identical**, e.g. (mono-1,
turns 6-12):

```json
{"action": "read_file", "path": "src/download_service_v2/jobs.py", "start_line": 1, "end_line": 30}
```

`jobs.py` is exactly 30 lines post-edit -- the model was not trying to see
more of the file; it reissued an already-answered request verbatim. The
corresponding `AgentTurnRecord.evidence_ids` is `[]` every time.

## What the model saw immediately after its edit

Reconstructed the real turn-6 prompt (mono-1). `SESSION_HISTORY_JSON`
correctly shows the edit accepted. `REPOSITORY_EVIDENCE` contained 10
items, including a **stale pre-edit copy** of `jobs.py:1-30` from the
original context-compiler package (`EV-006`) *and* the **fresh post-edit
copy** from the model's own read (`EV-009`), plus the real current diff
(`EV-010`) -- both file copies present side by side with no staleness
label. `CONFIGURED_VERIFICATION_COMMANDS_JSON` (the exact check names)
was present, verbatim, in every single turn's prompt, including every
turn the model ignored it.

This duplication is real but **not unique to the D4b fixture**: the
identical stale/fresh pairing was found in a previously preserved,
successful, completed session (`priority-a-64k`) that went on to call
`run_check` and complete. It is a pre-existing harness characteristic,
not a new defect introduced by this fixture, and therefore not, by
itself, an adequate explanation for why this fixture loops and others do
not. See ADR 0029's "Deferred follow-ups" for the disposition.

## Planned vs. monolithic prompt differences

Real: the planned condition's derived specification is genuinely smaller
(1 acceptance criterion / 2 hard constraints vs. 3/3 for monolithic).
Non-difference: `CONFIGURED_VERIFICATION_COMMANDS_JSON` lists all three
`v2-*-tests` names in *both* conditions (the catalog is project-wide, not
slice-scoped), so even the "simple" planned task shows two irrelevant
check names alongside the one it needs. Neither difference explains the
loop: it occurs identically regardless of specification size, and the
smaller planned prompt looped exactly as hard as the larger monolithic
one.

## `search_repository`'s `[WinError 2]` — real, but not the cause

Reproduced directly on this machine: `shutil.which("rg")` returns `None`
from a plain Python process, even though an interactive shell resolves
`rg` some other way (not a literal PATH-discoverable executable for
`CreateProcess`). `agent/inspection.py`'s `RepositoryInspector.search()`
has no fallback -- unlike `context/compiler.py`'s own `_ripgrep_search`,
which degrades to a lexical fallback -- so every `search_repository` call
fails deterministically with this error.

This affected only the three monolithic runs (each tried it exactly once,
at turn 2, before the loop began) and not the three planned runs, which
never called `search_repository` at all and looped identically. This is
a clean natural control: **the search failure did not cause or
contribute to the read loop.** It is a real, separate, reproducible
defect worth fixing on its own (see ADR 0029's deferred follow-ups), and
the original D4b report's "no retrieval failure" statement, while
narrowly defensible (nothing the model needed was ever unreachable),
should be read alongside this finding rather than as a claim that
`search_repository` works on this machine.

## Comparison with prior Qwen runs — the decisive finding

Every previously preserved live Qwen3-Coder-Next Q4 session under
`.apoapsis-eval/` (`local-strict-{1,3,4,5,6}`, `smoke-local`,
`priority-a-64k`/`-run2`/`-run3`, `priority-a-128k-run2`/`-run3` -- ten
sessions, same model digest `ca06e9e4087c...`, same
`temperature=0.0`/`think=false`) reliably transitions from an edit to
`inspect_diff`/`run_check` within one or two turns. Even the four that
still exhaust budget without completing (`local-strict-1`,`-3`,`-4`,`-6`)
spend their remaining turns cycling through `run_check`/`inspect_diff`/
`replace_text` -- `local-strict-1` alone calls `run_check` seven times --
never stuck reissuing one identical action for five or more consecutive
turns.

**The frozen-loop behavior is 6/6 reproducible only on
`download-service-v2`, in both conditions, and nowhere else in the
preserved evidence.** This rules out "the model never calls verification"
as an explanation and points at something specific to this fixture/task
content (or an interaction with prompt size -- `download-service-v2`
prompts run roughly 22-31KB vs. roughly 13KB for the smaller fixture,
though still far below the 65,536-token context window, so truncation is
ruled out) rather than the harness mechanics, the planning-vs-monolithic
framing, or the search failure.

Prompt-schema/action-format compliance was never in question: no session
recorded an `invalid_action` turn, and every telemetry record shows
`structured_output_valid: true`.

## What this evidence does and does not support

**Confirms** the D4b report's "model-logic failure" classification with
turn-by-turn, byte-level evidence.

**Adds** a finding not available at D4b review time: the loop is
fixture-specific, not general model behavior -- the same model verifies
reliably elsewhere under the identical harness/prompt structure.

**Does not** identify the specific mechanism (a decoding attractor under
greedy/`temperature=0.0` sampling on this particular prompt content, a
prompt-shape issue specific to this fixture, or something about
`jobs.py`'s specific content/instructions). Distinguishing these is
exactly what the D4c controlled probes (ADR 0029) are designed to do.

## Next steps

See ADR 0029 for the diagnostic-probe infrastructure this document
motivated, and `NEXT_STEPS.md`/`HANDOFF.md` for the current status of
each proposed live command.

## Live evidence addendum (2026-07-20)

Two of the probes this document motivated have since been run once each
against `qwen3-coder-next:q4_K_M` on `SLICE-JOBS-001`
(`download-service-v2`), the same slice D4b exercised. Values below are
read directly from the persisted `.apoapsis-eval/d4c-probe2-output/
diagnostic-probe.json` and `.apoapsis-eval/d4c-probe-control-output/
diagnostic-probe.json` artifacts and verified against them before being
written here; see `docs/adr/0029-d4c-diagnostic-probe-infrastructure.md`'s
own live-evidence addendum for the infrastructure-level framing.

| | Progress-advisory probe (Probe 2) | Unmodified-production control |
| --- | --- | --- |
| `prompt_condition` | `progress_advisory` | `production` |
| `model` | `qwen3-coder-next:q4_K_M` | `qwen3-coder-next:q4_K_M` (same configured model, same slice) |
| Turns | 8 | 5 |
| `v2-jobs-tests` runs | 1 (passed) | 1 (passed) |
| `AC-JOBS-STATE` | proven | proven |
| `outcome` | `COMPLETE` | `COMPLETE` |
| Input / output / cached tokens | 53,039 / 876 / 0 | 31,965 / 803 / 0 |
| Latency | 151.4s | 109.4s |

**Both probes escaped the exact read loop this document diagnosed**: each
made its one accepted edit, then inspected the diff and invoked
`v2-jobs-tests`, which passed, reaching real slice-level `COMPLETE` with
`AC-JOBS-STATE` proven against the configured acceptance command --
neither is a partial or simulated result.

**The unmodified production control succeeded without the advisory
prompt, and did so in fewer turns than the advisory condition.** Taken at
face value, these two single observations therefore give no basis for
attributing either success to the advisory note, and no basis for
changing the production prompt (`_AGENT_STEP_STATIC_PREFIX` remains
untouched by this milestone). What they do establish is that Qwen3-Coder-
Next Q4 *can* solve and verify `SLICE-JOBS-001` -- this document's read
loop is not a hard, unconditional capability limitation of this model on
this task.

That leaves the central puzzle open, not closed: **the contrast between
D4b's 0/6 and these two 2/2 successful observations is itself
unexplained.** Both probes ran through the same `VerticalSliceRunner
.execute_approved_task()` path this document already argued is equivalent
to D4b's `start_slice` path (ADR 0029), and the production-condition
control used the identical, unmodified prompt D4b's three planned
attempts used -- yet it did not loop this time. Plausible, currently
unmeasured explanations include run-to-run sampling or scheduling
sensitivity even under `temperature=0.0`/`think=false` (a fixed seed does
not guarantee byte-identical Ollama output across process restarts or
model reloads), and some difference in probe setup or worktree state
between this and the D4b runs. Distinguishing between these was not
attempted here and requires further, separately authorized runs.

**Scope of what was tested**: both observations cover only
`SLICE-JOBS-001`, the first slice of the three-slice `download-service-v2`
plan -- not the full plan, and not the held-out cross-slice oracle. No
completion rate, reliability rate, planning-vs-monolithic advantage, or
causal prompt effect is claimed from two single-run observations in
either direction. The two independent, non-causal defects recorded above
(`search_repository`'s `[WinError 2]`, unlabeled stale/fresh evidence
duplication) remain unresolved and were not touched by these runs.

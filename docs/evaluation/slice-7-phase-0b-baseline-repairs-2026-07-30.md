# Slice 7 Phase 0B: repairing the ruler

Date: 2026-07-30. **No inference.** Pre-qualification baseline repairs.

These are **not** Capability Sandbox wins. They are defects in the measuring
instrument, found while trying to freeze a baseline to measure against, and
fixed so that the qualification's own release gate means something.

## Result

> **Phase 0C followed and closed the remaining two.** Linux + Python 3.12 is now
> **1631 passed, 11 skipped, 0 failed.** See the classification at the end of
> this record.

| | Before | After 0B | After 0C |
|---|---|---|---|
| Linux + Python 3.12, full suite | 6 failed, 1625 passed | 2 failed, 1629 passed | **0 failed, 1631 passed** |
| Windows + Python 3.12, collection | **aborted the entire run** | **succeeds** |
| Windows + Python 3.12, `test_workcell_relay.py` | not reached | 37 passed, 20 skipped |
| Windows + Python 3.12, full suite | not reached | reaches ~4% then stalls — **new, separate finding, see below** |

Four of six repaired. Two remain and are **not** being changed without an
individual justification — see the last section.

## 1. Absolute destination directory accepted (`desktop/import_service.py`)

`preview_import` ran `destination_relative_dir.strip("/")` **before** calling
`is_safe_destination_relative_path`, whose first rule is
`startswith("/") -> False`. So `/etc` became `etc` and passed the check that
exists to reject it.

The validator was correct throughout. The caller destroyed the evidence before
validating. Fixed by validating the raw input first and normalising only
trailing separators afterwards, which are genuinely cosmetic.

**This is an authority-boundary defect**, not a cosmetic one: the check is what
keeps an operator-chosen import destination inside the project root.

## 2 & 3. `.git` and `.apoapsis` not excluded (`desktop/import_service.py`)

Exclusion was evaluated against `relative_destination`. For an explicitly named
source file, that string is `<dest>/<basename>` — so a file chosen from inside
`.git` arrived at the exclusion check as `HEAD`, with the `.git` parent already
discarded. `hard_exclusion_reason` never saw a directory to exclude.

Walked directories were pruned correctly (`_walk_directory` prunes excluded
names before descending), so the hole opened only for the case an operator is
most likely to hit: picking the file directly.

Fixed by giving `_CandidateFile` an `exclusion_probe` — the immediate parent
plus the basename — and checking exclusions against that. Only the immediate
parent: walking further up would start matching the operator's own directory
names, which have nothing to do with what is being imported.

**Also an authority-boundary defect.** A `.git` directory copied through the
import path is exactly what the containment work exists to prevent.

## 4. Read-loop detector blind to refused turns (`evaluation/diagnostic_probe.py`)

`first_no_progress_turn` required `item.accepted`. The harness has since gained
two improvements: it *refuses* a repeated inspection that adds no evidence, and
it stops a session after three such refusals. Both are good. But a refused turn
is recorded with the **rejection message** as its summary and `accepted=False`,
so every turn in a genuine read loop was excluded from the detector, which then
reported `None` while the model sat in precisely the loop the D4b forensic
analysis was written about.

Fixed by dropping the `accepted` requirement. A refused repeated inspection is
the strongest available evidence of no progress — the harness refused it
*because* it added nothing. The verification actions the docstring warns about
are excluded by `_NO_PROGRESS_ACTIONS` and are unaffected either way.

### One assertion changed, justified individually

`test_summary_reports_the_read_loop_when_the_model_never_verifies` asserted
`max_identical_action_streak >= 4`. After the detector fix it reports **3**.

That bound is now **structurally unreachable**, not merely unmet. The session's
own recorded stop reason is *"coding model repeated prohibited no-progress
repository observations **three times** without making progress"* — so a fourth
identical turn cannot exist, by the product's own rule. The old bound was
written when the loop ran to the turn cap.

Changed to `assertEqual(..., 3)` rather than relaxed to `>= 3`: the number *is*
the three-strikes rule, and a test tolerating 4 would stop noticing if the stop
ever regressed to firing late.

This is the only expectation changed anywhere in Phase 0B.

## 5. Relay unimportable on Windows (`workcell/relay.py`)

`class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer)` was evaluated at
import time, and that attribute does not exist on Windows. The failure was not
contained: `tests/test_workcell_relay.py` failed during **collection**, which
aborts the whole pytest run, so the complete deterministic suite could not be
executed on a Windows host at all — while the release gate reads *"the
deterministic suite must add no failures."*

Fixed by resolving the base class with `getattr(..., object)` and raising
`UnixSocketUnsupportedError` at **construction**. Import and collection work
everywhere; the capability is not faked. The tests already carried skip guards
for Unix-only execution, so nothing else was needed: Windows now reports
37 passed, 20 skipped for that module.

## The qualification matrix

| Platform | Scope | Expectation |
|---|---|---|
| **Linux + Python 3.12** | Complete deterministic suite | **Must pass.** This is the gate for Phase 1 |
| **Windows + Python 3.12** | Host-compatible suite; collection must succeed | Unix-socket execution skipped explicitly, never silently absent |

Linux is the declared platform for the release gate. Windows must remain
*collectable* so that a host-side regression is visible rather than hidden
behind an aborted run.

### A new finding the fix exposed: the Windows run stalls

With collection unblocked, the full Windows suite now reaches roughly 4% and
then stops progressing — observed for over ten minutes with no further output.
The two `F`s visible before the stall are the known `acceptance_coverage` pair.

This was **invisible before**, because collection aborted first. It is a real
host-side defect and it is recorded rather than fixed: diagnosing a hang is
unbounded work, it is outside the "fix only what these tests prove broken"
remit, and the Linux leg is the gate for Phase 1.

What the matrix therefore requires today is honest about it: **Windows
collection must succeed**, which it now does, and the host-compatible suite is
**not yet demonstrably runnable to completion**. Closing that is a bounded
follow-up — most likely a single subprocess test without a timeout — and it
should be done before Windows is claimed as a supported test platform rather
than merely a supported host.

## Still open: the two stale-evidence failures

`test_stale_worktree_digest_result_does_not_prove_current_code` and
`test_untracked_new_file_creation_invalidates_earlier_proof` both fail with
`COMPLETE != HUMAN_REVIEW_REQUIRED`.

**The digest scoping itself is working.** Instrumenting the first test shows the
fingerprint bumping correctly (`fb52e410 -> ed85dd79`), and shows
`compute_acceptance_coverage` receiving exactly the current-digest results at
each step — including the step that matters, where AC-1 is correctly `unproven`
at the new digest with reason *"has not yet been executed for the current
worktree state"*.

The final report differs for a different reason. `_final_verification_passed`
runs the **full configured command set** at the end of every session, because
the model's own completion claim carries no authority. That re-executes
`download-tests` at the new digest, which legitimately re-proves AC-1 with
current evidence, and the task completes.

So on the evidence gathered, the product looks *correct* and the test's outcome
assertion looks obsolete — it was written for a world where the session ended
without that final sweep.

**I am not making that change.** Two reasons:

1. The claim "the product is fine, the test is obsolete" resolves in the
   flattering direction, and it is the same direction two earlier errors in this
   programme resolved in. It deserves a second reader, not a self-review.
2. These two tests guard the exact property — stale evidence must not prove
   current code — that Slice 7 exists to measure. Changing their expectations
   as part of preparing to run Slice 7 is a conflict of interest even when the
   reasoning is sound.

**Owner decision needed.** If the analysis above is accepted, the correct repair
is to assert the intermediate coverage state (where AC-1 *is* `unproven` at the
new digest) rather than the final outcome, preserving the test's intent while
matching the product's improved behaviour. That is a change to the test's
mechanism, not just its expected value, which is more than Phase 0B's remit
allows without explicit approval.

## Phase 0C: the two tests corrected, invariant preserved

Approved and done. Both tests now assert the invariant as the **sequence** it
actually is, via `_assert_stale_digest_sequence`:

1. **Digest-A evidence cannot prove digest B.** Asserted against
   `compute_acceptance_coverage` directly — the public function whose
   documented contract *is* the invariant — using the criteria the run itself
   parsed, not a hand-built stand-in.
2. **Visibly unproven before the sweep.** Given a results map with no entry for
   the mapped command (exactly the digest-B state before the re-run), AC-1 is
   `UNPROVEN` with reason *"has not yet been executed for the current worktree
   state"*.
3. **The sweep may re-run and produce digest-B evidence.** Asserted from the
   durable report: the mapped command appears **at least twice** across
   `verification_results`, and the proving execution's `started_at` is strictly
   after the first execution's `finished_at` — so it post-dates the mutation
   rather than preceding it.
4. **Success must cite the new evidence.** Final coverage is `PROVEN` with
   reason *"for the current worktree state"* and `evidence_reference` naming the
   mapped command.

Two further assertions the owner asked for:

- **The digest genuinely moved** — `compute_worktree_fingerprint` on the report's
  own worktree must show a non-empty tracked diff against HEAD or untracked
  files present. Not a bare "is it a sha256" check.
- **The old evidence cannot be smuggled back** — a results map carrying a pass
  for a command that is not a configured acceptance check still leaves the
  criterion `UNPROVEN`. The only path to `PROVEN` is an approved acceptance
  command executed for the current state.

Nothing was weakened: digest matching, the end-of-session verification sweep,
and the three-strikes rule are all untouched. No product code changed in 0C.

## Classification

| Item | Class |
|---|---|
| Absolute destination accepted | **baseline ruler repair** |
| `.git` not excluded | **baseline ruler repair** |
| `.apoapsis` not excluded | **baseline ruler repair** |
| Read-loop detector blind to refused turns | **baseline ruler repair** |
| `test_stale_worktree_digest_result_does_not_prove_current_code` | **obsolete test mechanism; original stale-evidence invariant preserved** |
| `test_untracked_new_file_creation_invalidates_earlier_proof` | **obsolete test mechanism; original stale-evidence invariant preserved** |
| Relay unimportable on Windows | **baseline ruler repair** |

**None of these is a Capability Sandbox win.** They are repairs to the
instrument, made so that the qualification's own release gate — *"the
deterministic suite must add no failures"* — measures something real. Counting
any of them toward the harness defect-detection claim would be counting the
ruler's calibration as a measurement.

Linux is green. Phase 1 is unblocked. **No live inference has been run.**

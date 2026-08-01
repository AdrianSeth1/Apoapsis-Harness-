# Slice 4: slice readiness and structured witnesses

Date: 2026-07-30  
Evidence class: **deterministic only.** 48 new tests. No model calls, no
container. This slice changes what "done" means; it does not run anything.

## The exit criterion, met

> The exact Crisis Atlas Slice 2 first proposal cannot complete even when all
> inherited tests pass, and a real server/routes witness can prove the slice.

`CrisisAtlasSlice2Tests` reconstructs that proposal exactly: one partial file
at `services/incident_service.py` — the wrong package path — no export service,
no new tests, and an inherited suite that passes with coverage naming only
`incident/domain.py` and `incident/persistence.py`.

The readiness evaluation returns **not ready**, with three independent blocks:

| Block | Why |
| --- | --- |
| `MISSING_REQUIRED_ARTIFACT` | the declared package path `incident/services/incident_service.py` is absent — the wrong-path service |
| `OBLIGATION_UNPROVED` | `ExportService` was never created |
| `NEW_COMPONENT_UNEXERCISED` | the file that *was* written is reached by nothing |

A companion test asserts the required command **did** pass and the slice is
still not ready. That is the whole correction: ADR 0069's rule was "all
configured checks are green", and green is present here.

A fourth test shows the correct slice — both services in their declared
packages, a witness whose coverage names both — evaluating **ready**.

## Why inherited green was never evidence

The inherited suite in the fixture passes *because* its coverage does not
include the new file. Greenness was evidence that nothing had changed, and the
harness read it as evidence that everything had. The coverage section is what
makes that visible: a witness now has to say which paths it actually reached,
and `NEW_COMPONENT_UNEXERCISED` fires on any added production path that appears
in no passing witness's coverage.

## Structured witnesses replace command names

> A command named `behavioral-integration` is not evidence that integration
> occurred.

`validate_witness` refuses a witness whose only content is its own name and an
exit code (`COMMAND_NAME_ONLY`), and seven other ways a witness can fail to be
evidence:

- **`STALE_FINGERPRINT`** — every witness names the worktree it observed. One
  from before the last edit describes code that no longer exists.
- **`NO_COVERAGE_METHOD`** — coverage asserted without saying how it was
  collected is indistinguishable from a hand-written list.
- **`LAUNCH_WITHOUT_READINESS`** — a launch with no readiness condition may
  have raced the server it was testing. "We slept three seconds" has to be
  written down as such.
- **`LAUNCH_NOT_CLEANED_UP`** — a server left running can make a *later*
  witness pass for the wrong reason.
- **`MUTATION_NEVER_RE_READ`** — a POST nobody read back proves the endpoint
  accepted a request, not that anything persisted. Crisis Atlas shipped exactly
  that shape.
- **`FAILED_WITNESS_CLAIMS_PROOF`** — a failing witness listing criteria proved.
- **`SCHEMA_VERSION_UNKNOWN`** — fields that cannot be read with confidence.

`require_witness` fails closed: a wrapper that cannot produce its declared
evidence must emit nothing, because a witness with a missing section is
indistinguishable from one whose section found nothing.

## The contract has to be satisfiable

`SliceAcceptanceContract` refuses two shapes at construction:

- a **criterion with no obligation** that could prove it — otherwise the
  contract is unsatisfiable in a way nobody notices until delivery;
- an **obligation naming nothing** that could discharge it — "it should be
  good" can be neither proved nor disproved.

`EvidenceClass` separates `INDEPENDENT`, `MODEL_AUTHORED`, and `INHERITED`. An
obligation may set `requires_independent_evidence`, and model-authored tests
then cannot discharge it. The unrestricted control wrote 87 passing tests and
still shipped a broken status filter; its own tests helped it build and did not
independently prove the product.

An `unmeasured_reason` does **not** satisfy an obligation. It records the
owner's statement and routes the slice to human review — silence becomes a
visible statement rather than a pass.

## `CheckpointOutcome.CONTINUE` is the outcome Slice 2 never had

`evaluate_checkpoint` takes an admission result and a readiness report. It
takes **no command results at all** — a test asserts its signature, because
greenness must be unable to reach a completion decision except through
readiness, several layers down.

Its four outcomes:

- `COMPLETE` — admitted *and* ready.
- **`CONTINUE`** — admitted, obligations outstanding, the agent gets another
  turn. This is precisely what Crisis Atlas Slice 2 was denied: Qwen stated a
  plan for two services and tests, produced one partial file, and never
  received a turn in which it could notice its own omissions.
- `CANDIDATE_REFUSED` — admission said no; nothing was promoted.
- `HUMAN_REVIEW_REQUIRED` — an intentionally unmeasured obligation, which no
  further model turn can fix and which would otherwise loop.

## Honest limitations

- **Nothing emits these witnesses yet.** The schema, the validation, and the
  readiness rule exist; the wrappers that turn `python -m unittest` or a
  browser run into a `StructuredWitness` are not built. Until they are, the
  rule is enforceable in principle and unenforced in practice.
- **No contract compiler.** `SliceAcceptanceContract` is hand-constructed in
  tests. Compiling one from an approved plan slice — which the handoff wants
  done *before model spend* — is not implemented.
- **Coverage is taken on trust from the witness.** `collection_method` forces
  the wrapper to say how it measured, but nothing here re-derives coverage
  independently. A wrapper that lies produces a witness that validates.
- **The new-component rule covers added production paths only.** A modified
  file that gains an entirely new function is not treated as a new component.
  That is deliberate — inherited tests may legitimately already exercise the
  file — but it is a real gap: Crisis Atlas Slice 3's unreachable export routes
  lived in a *modified* file.
- **Not wired into any live path.** No session calls `evaluate_checkpoint`
  yet; ADR 0069's termination still governs the legacy Local Power loop.
- `relay.py` still cannot be imported on Windows (Slice 2A defect, unfixed).

## Verification

`compileall` clean. `tests/test_workcell_acceptance.py` 24 tests. Focused set —
acceptance, admission, agent profile, workcell, paired scoring — **187 passing**
(1 skipped where symlinks are not creatable). `git diff --check` clean.

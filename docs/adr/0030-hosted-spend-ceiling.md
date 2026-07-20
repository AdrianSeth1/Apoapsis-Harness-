# ADR 0030: Hosted-frontier spend ceiling (D5b readiness)

- Status: Accepted
- Date: 2026-07-20

## Context

D5b's goal is a controlled comparison between local-first and direct-hosted-
frontier evaluation lanes (`apoapsis eval download-service --lane frontier/
hybrid/forced-escalation`, ADR 0006/0012/0028), using the user's own hosted
API credentials once explicitly authorized. The user's own subscription
access (ChatGPT/Claude web) does not imply API credentials or included API
usage, and no automation of a subscription web session as a provider API is
in scope here or anywhere else in Apoapsis.

Before any live hosted call is authorized, hosted evaluation readiness must
be made deterministic: configuration must be verifiable without printing
secrets, credentials must remain environment-variable references, every
call must already record model/tokens/cache/latency/cost/failure (ADR
0002's instrumentation wrapper already does this), and -- new in this
milestone -- a run must never be able to spend past an amount the user
explicitly approved, checked both before and after every hosted call, with
no silent local-to-hosted fallback anywhere in the process.

## Decision

### Scope: evaluation-only, one CLI flag, no change to production execution

Every new capability lives in `apoapsis.evaluation.spend_ceiling` and one
new `apoapsis eval --max-hosted-spend-usd <AMOUNT>` flag. Production task
execution (`apoapsis run`/`apoapsis execute`) is untouched -- it already has
its own authorization boundary (ADR 0022's explicit human-authorized fresh
frontier stage) for a different problem (authorizing *that a hosted call
happens at all*, not bounding *how much a whole run may spend*). This
milestone is specifically about the D5b comparison harness, where a single
invocation can make many hosted calls across several lanes.

### Two independent checks, not one

`SpendLedger` (a small, shared, mutable per-invocation record) enforces the
ceiling with two checks that never trust each other's absence:

1. **Before every call** (`refuse_if_worst_case_exceeds`): a deliberately
   pessimistic worst-case cost estimate for the call about to be made --
   the full uncached input-token estimate (never the cheaper cached price)
   plus the full configured `max_output_tokens` ceiling, even though a real
   response is almost always shorter -- is compared against the ceiling
   minus what has already been spent. If the prospective total would
   exceed the ceiling, the call is refused *before it is made*; `spent_usd`
   is never mutated by a refusal.
2. **After every call** (`record_actual`): the real, recorded
   `estimated_cost_usd` from `InstrumentedModelProvider`'s own telemetry
   (ADR 0002) is added to `spent_usd`; if that pushes the total past the
   ceiling, this raises too -- a hard backstop for the case a worst-case
   estimate ever turns out to have been wrong, or a real call happened to
   report an atypically expensive result.

Both raise `HostedSpendCeilingExceededError` and set a sticky
`SpendLedger.exceeded` flag. Neither response to an exceeded ceiling is
ever "retry with a cheaper request," "silently use a different model," or
"truncate output and continue" -- refusal is the only response.

### Whole-run refusal before anything starts

`estimate_worst_case_run_cost_usd(config, lanes)` computes a pessimistic
upper bound for an entire `apoapsis eval` invocation directly from
configuration -- no prompt has to exist yet, no provider is built, no
fixture is copied: for every requested lane that needs the frontier coder,
`frontier_agent.max_turns` calls at the configured context budget and
`frontier_coder.max_output_tokens` ceiling. `_eval_download_service`
(`cli/app.py`) refuses the entire run before starting the first lane if
this exceeds `--max-hosted-spend-usd`, and separately refuses outright if
any requested lane needs the frontier coder and `--max-hosted-spend-usd`
was not supplied at all -- there is no default ceiling and no way to make
a hosted call without one.

### Wrapping, not replacing, the existing instrumentation

`SpendCeilingModelProvider` wraps an `InstrumentedModelProvider` (never
replaces it) and is passed as `frontier_coder_provider` to the existing,
unmodified `run_eval_lane`/`VerticalSliceRunner` exactly where an
unwrapped `InstrumentedModelProvider` would go -- `VerticalSliceRunner`
only ever calls `.provider_name`/`.model_name`/`.complete(...)`, so no
change was needed there at all. If a ceiling breach happens mid-lane,
`VerticalSliceRunner`'s existing broad failure handling (`_handle_failure`)
catches it like any other provider error and reports that one task as
failed -- correct for that lane's own report, but not sufficient on its
own to stop the whole invocation. `_eval_download_service`'s lane loop
additionally checks `spend_ledger.exceeded` after every lane completes and
raises immediately if set, so a breach during lane *N* guarantees lane
*N+1* is never started.

### Visibility before spending, without breaking the CLI's output contract

`apoapsis eval`'s stdout is exactly one JSON object (the final comparison
report) by existing convention -- several tests parse it that way. The
"show the exact planned number of calls, models, maximum tokens, and spend
ceiling before starting" requirement is satisfied without breaking that
contract: the plan (hosted lanes, model, `max_calls_per_lane`,
`max_output_tokens_per_call`, the computed worst-case total, and the
ceiling) is written to `hosted-spend-plan.json` in the run's output
directory and also printed as one line to **stderr**, never stdout, before
the first lane starts. After the run, actual totals (`spent_usd`,
`calls_recorded`, `calls_refused`, `exceeded`) are written to
`hosted-spend.json` alongside the existing `comparison.json`/`.md`.

### D5b readiness beyond the ceiling itself

- `apoapsis doctor` gained `_hosted_pricing_checks`: a `WARNING` when a
  configured `openai_compatible` model role has every pricing field left
  at its zero default -- every recorded call's cost would then read $0
  regardless of real usage, silently defeating both the ceiling above and
  any cost reported in evaluation evidence.
- Credential handling is unchanged from ADR 0002/0004: `api_key_env`
  stores an environment-variable *name*, never a value; doctor's existing
  `_credential_checks` (and its dedicated regression test) already prove
  no check path ever includes the secret's value in a `DoctorCheck.detail`
  or `.remediation`.
- No hosted call is made anywhere in this milestone. Every test uses
  `FakeModelProvider` or pure functions; the CLI-level refusal tests use a
  deliberately unreachable `base_url` (nothing is ever dialed, since both
  refusal paths reject before any provider's `.complete()` is called).

## Non-goals

- Does not change `apoapsis run`/`apoapsis execute`'s existing
  ADR 0022 frontier-authorization boundary, or add a spend ceiling there.
- Does not make, or gate the making of, any live hosted call. That remains
  explicitly gated on the user's separate `HOSTED D5 AUTHORIZED` block
  (provider, exact model, credential environment variable name, maximum
  total spend, and authorized lanes), none of which this ADR grants.
- Does not add per-model or per-lane sub-ceilings, only one aggregate
  ceiling per invocation.
- Does not attempt to automate a ChatGPT/Claude subscription web session as
  a provider API -- hosted access here means only a configured
  `openai_compatible` (or hosted `ollama`-shaped) API endpoint and
  credential the user has separately obtained.
- Does not change token/cost accounting itself (ADR 0002's instrumentation
  wrapper), only adds a ceiling checked against its existing output.

## Tests

`tests/test_spend_ceiling.py` (22 tests, all deterministic): worst-case
call/run cost estimation (pessimistic-upper-bound math, zero when no
hosted lane is requested or no frontier coder is configured, scaling with
lane count and configured budget); `SpendLedger` (negative-ceiling
rejection, pre-call refusal never mutating spend, post-call accumulation,
post-call breach still recording the real cost, `remaining_usd` never
negative); `SpendCeilingModelProvider` wrapping a real
`InstrumentedModelProvider`+`FakeModelProvider` (a call within budget is
forwarded and recorded, a call whose worst case alone exceeds the ceiling
is never forwarded to the underlying provider at all, a second call can be
refused after a first succeeds, a failing inner call never corrupts the
ledger); two `run_eval_lane` integration tests proving the wrapper behaves
correctly through the real `VerticalSliceRunner` path a live FRONTIER lane
uses (a completing two-call run records real spend; a ceiling sized for
exactly one call stops the lane on the second, non-`COMPLETE` outcome,
`exceeded=True`); and three CLI-level tests on `_eval_download_service`
proving both pre-flight refusals happen before any fixture is copied or
any provider is touched, and that a local-only request never requires a
ceiling even when `frontier_coder` is configured.

`tests/test_doctor.py` gained `DoctorHostedPricingTests` (3 tests): the
new warning fires only for a hosted model with all-zero pricing, never for
one with real pricing configured, and never for a local-only Ollama model.

## Consequences

`apoapsis eval` can now request a hosted lane only with an explicit,
enforced aggregate spend ceiling: refused outright if omitted, refused
before any lane starts if the run's own configured worst-case allowance
already exceeds it, and hard-stopped mid-run (no further lanes started) if
real spend ever approaches it despite passing the pre-flight check.
`apoapsis doctor` can now warn that a hosted model's pricing is
misconfigured before that silently produces a useless $0 ceiling. No live
hosted call has been made; hosted metrics for D5b remain `not yet
measurable` until the user's separate `HOSTED D5 AUTHORIZED` block is
provided.

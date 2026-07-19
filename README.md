# Apoapsis Harness

Apoapsis Harness is a local-first context, research, and verification layer for AI
coding agents. It contains the deterministic `substrate-v0.1` baseline, a
bounded inspect-edit-test coding loop, the original one-shot patch baseline,
and a quarantined local Research Mode.

For a plain-English tour of how the system works—including what the held-out
oracle does and does not know—start with
[`docs/architecture-explained.md`](docs/architecture-explained.md). Coding
agents should start with [`HANDOFF.md`](HANDOFF.md) for the canonical living
architecture, current implementation status, known limitations, and required
maintenance contract. The ADRs remain the decision history; this README is the
user-facing guide.

Version 0.7 adopts the complete Apoapsis namespace: the distribution is
`apoapsis-harness`, the Python package and CLI are `apoapsis`, new project state
lives in `.apoapsis/`, product environment variables begin with `APOAPSIS_`, and
managed branches begin with `apoapsis/`. There is no pre-release compatibility
alias. Legacy `.sol/` audit directories remain excluded and read-only so their
content hashes and worktree pointers are not corrupted; see
[ADR 0007](docs/adr/0007-apoapsis-namespace.md).

## What works now

- Structured task, hard-constraint, context-evidence, model I/O, verification,
  and workflow-event schemas.
- Exact preservation of hard-constraint wording and fail-closed model-request
  constraint coverage.
- Atomic, optimistic SQLite workflow transitions with an append-only event log.
- Repository inspection and dedicated Git worktree/branch lifecycle.
- TOML-configured verification commands with timeouts, restricted environment,
  bounded logs, and structured results.
- A dependency-light CLI and standard-library test suite.
- Native loopback-only Ollama and authenticated OpenAI-compatible frontier
  adapters with token, cache, latency, and configured-price telemetry.
- Model-assisted specification extraction with exact hard-constraint source
  validation and explicit user approval.
- Reproducible Git/ripgrep/symbol/import/test context packages with line-level
  provenance.
- A typed coding-agent protocol for literal search, bounded reads, diff
  inspection, incremental patches, configured checks, full verification, and
  explicit escalation—with no shell or arbitrary command access.
- Unified-diff parsing, policy validation, safe worktree application, bounded
  iteration, and verifier-owned completion.
- Deterministic risk routing across local-only, local-then-frontier,
  frontier-only, and human-review paths, with a reproducible escalation package
  and separate budgets for each coding stage.
- A complete per-task audit directory and aggregate usage/outcome report.
- Deterministically triggered GitHub, official-documentation, and opt-in Reddit
  research planned and synthesized by a tool-free local model.
- Native Ollama structured output, thinking controls, model digest, token, and
  duration telemetry, with the OpenAI-compatible interface retained as fallback.
- Source provenance, license classification, content quarantine, injection
  warnings, bounded caching, comparative synthesis, and brief-only frontier
  handoff.
- A read-only `apoapsis doctor` preflight (toolchain, configured models,
  context limits, credential presence, verification commands, and an opt-in
  provider connectivity probe) and an `apoapsis eval` harness that runs every
  execution lane against a fresh copy of a controlled fixture and writes one
  comparison report.
- Windows `START_APOAPSIS.cmd`/`STOP_APOAPSIS.cmd` controls that derive local
  Ollama models from configuration, warm the coding model, and explicitly
  release every configured local model's memory without touching hosted providers.
- An offline black/orange/purple local operator interface for real repository,
  task, specification, plan, Human Review, event, report, evaluation, and
  model-configuration data, including version-checked specification/plan
  approval, bounded continuation, crash recovery, and explicit fresh-frontier
  authorization.

See [ADR 0001](docs/adr/0001-mvp-deterministic-substrate.md) for the substrate
and [ADR 0002](docs/adr/0002-frontier-vertical-slice.md) for the frontier flow.
[ADR 0003](docs/adr/0003-local-research-mode.md) records the Research Mode trust
boundary, [ADR 0004](docs/adr/0004-native-ollama-frontier.md) records the native
all-local proposal path, [ADR 0005](docs/adr/0005-bounded-coding-agent-loop.md)
records the agent action boundary,
[ADR 0006](docs/adr/0006-deterministic-frontier-escalation.md) records provider
routing and escalation, [ADR 0007](docs/adr/0007-apoapsis-namespace.md) records
the product/runtime namespace migration, and
[ADR 0008](docs/adr/0008-evaluation-and-diagnostic-tooling.md) records the
evaluation harness and diagnostic tooling contract,
[ADR 0009](docs/adr/0009-execution-sandbox.md) records the execution
sandbox, [ADR 0010](docs/adr/0010-context-measurement-and-wider-profiles.md)
records the 128k/256k context profiles and the deterministic context-
measurement layer,
[ADR 0011](docs/adr/0011-deterministic-context-quality.md) records change/
reference/failure-directed retrieval, bounded observation compaction, and
stable prompt prefixes, and
[ADR 0012](docs/adr/0012-held-out-oracles-and-evaluation-aggregation.md)
records held-out correctness checks and cross-run metrics, and
[ADR 0013](docs/adr/0013-local-model-operator-lifecycle.md) records safe local-
model Start/Stop behavior, and
[ADR 0014](docs/adr/0014-local-operator-interface.md) records the local
application/API and browser-session security boundary. The
[Research Mode guide](docs/research-mode.md)
covers setup and operation.

The owner and coding-agent roadmap is [`NEXT_STEPS.md`](NEXT_STEPS.md). The
standalone black/orange/purple application brief for Claude Design is
[`docs/product-design-handoff.md`](docs/product-design-handoff.md).

## Install for development

Requirements are Python 3.12+, Git, and preferably ripgrep. The context compiler
has a deterministic lexical fallback when ripgrep is unavailable.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .
```

On macOS or Linux, use `.venv/bin/python` instead.

Run the tests:

```bash
python -m unittest discover -s tests -v
```

## Start and stop local models on Windows

Double-click `START_APOAPSIS.cmd` before a local session. It validates the
configured loopback Ollama endpoint, starts the default local service if needed,
checks that models are already installed, and warms the deduplicated coding model
for 30 minutes at its configured context size. It never pulls a model.

The research-only model stays lazy by default because loading two large models
can exceed available RAM/VRAM. Warm it explicitly when needed:

```powershell
.\START_APOAPSIS.cmd --include-research
```

When finished, double-click `STOP_APOAPSIS.cmd`. It sends an explicit zero keep-
alive to every configured local Ollama model, including research, and releases
their memory. The shared Ollama service remains running intentionally; hosted
providers, Docker, repositories, worktrees, and tasks are untouched.

For terminal automation, set `APOAPSIS_NO_PAUSE=1` so the command files do not
wait for a keypress. The last lifecycle result is recorded under the ignored
`.apoapsis/runtime/` directory.

## Local operator interface

Launch the offline interface from an initialized project:

```powershell
apoapsis ui
```

It opens a capability-protected loopback session at `127.0.0.1:7331`. Use
`apoapsis ui --no-open` to serve without opening a browser, or `--port` to select
a different loopback port. All HTML, CSS, and JavaScript assets ship with
Apoapsis; the interface contacts no CDN and never calls a model provider
directly.

The first slice provides:

- Home/project status and persisted tasks;
- specification review with exact verbatim hard constraints;
- a two-step, optimistic-version specification approval that writes the same
  workflow event as `apoapsis approve`;
- workflow timelines, change/verification summaries, final usage reports, and
  audit-artifact locations;
- persisted evaluation comparisons and actual configured model roles;
- a **Plans** index and detail view (ADR 0019): architecture summary,
  decisions, dependency-ordered implementation slices, validation findings,
  package/provenance, and a deterministic, optimistic-version-checked
  approve action that states explicitly it does not execute any slice;
- a **Human Review** queue and case-detail view (ADR 0020): exact stop
  reason, current diff, active constraints, verification/acceptance
  results, consumed vs. configured budgets, and only the actions the
  review service actually declares eligible, each behind two-step
  confirmation. Submitting an action returns immediately -- a background
  worker performs the actual work (a resumed model call, a verification
  run, or a worktree cleanup), and the page polls a persisted operation id
  for progress, surviving a reload without resubmitting; and
- an explicit **Run doctor** action. Merely opening the UI does not probe or
  load a model.

Natural-language task extraction and workflow execution for new tasks, and
plan-slice execution, are visibly unavailable in this first slice. Continue
using the CLI for those operations until their resumable application
services and deterministic transition contracts are implemented. The
supplied Claude Design export is a visual reference only; its external
prototype runtime is not shipped.

## Current CLI workflow

Initialize Apoapsis inside an existing Git repository:

```bash
apoapsis init
```

Draft a task without model inference. Repeated flags preserve constraints and
criteria as separate source-backed records:

```bash
apoapsis task "Add resumable downloads" \
  --constraint "Preserve the current public API." \
  --constraint "Do not add runtime dependencies." \
  --acceptance "Interrupted downloads resume from the persisted byte."
```

Review and approve the generated task ID:

```bash
apoapsis inspect TASK-ABC123
apoapsis approve TASK-ABC123 --version 2
```

The lower-level workflow APIs then support repository analysis, context
compilation, routing, patch readiness, and verification as later milestones are
added. Worktree and verification lifecycle commands already exist:

```bash
apoapsis worktree-create TASK-ABC123
apoapsis verify TASK-ABC123
apoapsis rollback TASK-ABC123 --delete-branch
```

`verify` deliberately refuses to run until the persisted task state is
`PATCH_READY`. `rollback` is explicit and may discard uncommitted task-worktree
changes. Normal cleanup APIs refuse dirty worktrees unless force is requested.

## Architect Mode: deterministic planning foundation (ADR 0019)

Architect Mode lets a stronger model (Claude, Codex, Fabel, or any other
model you already have access to -- manually, no new subscription or API
credential required) design an architecture and decompose a large idea into
small implementation slices sized for the local coding model's existing
bounded-agent loop. It never executes anything itself: it produces a plan, a
human reviews it, and only an explicit, version-checked approval action ever
changes its status.

```bash
apoapsis plan export "Add resumable downloads with a pluggable storage backend"
```

This writes an immutable `PlannerRequestPackage` (idea text, repository
identity, deterministic context evidence, the configured verification
catalog, documentation references, the plan JSON schema, and explicit
authority rules) to `.apoapsis/plan-packages/<package_id>/request-package.json`
and prints it. Paste the package into any capable chat model, ask it to
return an `ArchitecturePlan` matching the included schema, and save its
response (wrapped with `package_id` and `request_package_sha256`, matching
the package) to a file:

```bash
apoapsis plan import response.json
apoapsis plan validate PLAN-ABC123
apoapsis plan inspect PLAN-ABC123
apoapsis plan approve PLAN-ABC123 --expected-version 2
```

`plan import` rejects a response whose `request_package_sha256` does not
match the stored package exactly. `plan validate` runs deterministic checks
(unique IDs, no dependency cycles or missing dependencies, no unknown
constraint/criterion references, no invented verification-command names,
every active hard constraint represented in some slice, every slice names a
real configured verification command, configurable ceilings, and
repository-relative non-escaping suggested paths) and never raises for
content problems -- an invalid plan is still stored with concrete findings.
`plan approve` requires the plan's last validation to be valid and uses the
same optimistic-version discipline as `apoapsis approve`. A plan can never
mark itself approved or executed: `ArchitecturePlan` has no such field, and
approving a plan never executes any slice -- executing an approved slice is
explicitly out of scope for this milestone.

Validation ceilings are configurable under `[architect.ceilings]` in
`.apoapsis/config.toml` (`max_slices`, `max_dependency_depth`,
`max_suggested_paths_per_slice`, `max_criteria_per_slice`,
`max_work_brief_chars`); `apoapsis init` writes explicit defaults.

## Human review and resume (ADR 0020)

A task that stops at `HUMAN_REVIEW_REQUIRED` -- a rejected specification,
a routing decision that requires a human, incomplete acceptance coverage,
or an exhausted local/frontier coding agent -- now has a real, deterministic
resume path instead of a dead end:

```bash
apoapsis review list
apoapsis review inspect TASK-ABC123
```

`inspect` shows the exact stop reason, current diff, active constraints,
verification/acceptance results, consumed vs. configured budgets, and the
harness-computed set of actions actually available for this task --
never a fixed menu. Every mutation requires the task's current version, a
fresh worktree fingerprint (when a worktree exists), and an explicit,
caller-supplied `--operation-id`; resubmitting the same operation id is
always rejected, so a retried or ambiguous request can never silently
repeat a model call:

```bash
apoapsis review abandon TASK-ABC123 --expected-version 4 --operation-id RVOP-1
apoapsis review retry-verification TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> --operation-id RVOP-2
apoapsis review continue-local TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> \
  --operation-id RVOP-3 --additional-turns 6
apoapsis review continue-frontier TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> \
  --operation-id RVOP-4 --additional-turns 6
```

`continue-local`/`continue-frontier` resume the exact bounded agent session
that stopped -- same worktree, same prior turns and observations, same
verification history -- with only the authorized additional turns (and a
matching increase to patch-attempt/verification-run budgets) added on top
of whatever was already consumed; nothing is ever reset. `--additional-turns`
and the number of continuations per task are both capped by
`[review]` in `.apoapsis/config.toml` (`max_additional_turns_per_continuation`,
`max_continuations_per_task`). `continue-frontier` is only ever offered when
a frontier agent session already exists for that task; it never launches a
fresh frontier attempt from a local-only stop.

Starting a fresh frontier stage from a local-only stop is a distinct,
explicitly confirmed action (ADR 0022), never something `continue-frontier`
does implicitly:

```bash
apoapsis review authorize-frontier-stage TASK-ABC123 \
  --expected-version 4 --expected-fingerprint <digest> --operation-id RVOP-5
```

`authorize-frontier-stage` is only offered while a frontier coder is
configured and no frontier session exists yet for the task -- once one
does, only `continue-frontier` is offered from then on. It always uses the
full configured `[execution.frontier_agent]` budget (there is no
`--additional-turns` flag for it, since this is a new session, not a
continuation); both the CLI and the UI display the exact frontier model
and budget before it runs. Frontier availability is always checked against
the *current* configuration, not whatever was true at the original stop --
adding `[models.frontier_coder]` to `.apoapsis/config.toml` after a
local-only stop is enough to make the action available on the next
`review inspect`.

Every operation is re-validated against fresh state (task version, worktree
fingerprint, eligibility, budgets) immediately before it does anything,
never only at submission time (ADR 0021) -- and only one operation may be
active per task at once. If the process running a continuation is killed,
`apoapsis review recover` (also run automatically whenever `apoapsis ui`
starts) reclaims any operation that never actually started, marks a stale
in-progress operation as ambiguous (never automatically repeated), and
returns a stranded task to human review without claiming what the
interrupted call did:

```bash
apoapsis review recover
```

## Diagnostics and evaluation

Check the local toolchain, configured models, context limits, credential
presence (values are never printed), and verification commands:

```bash
apoapsis doctor
apoapsis doctor --probe
```

`--probe` makes one real minimal completion call per configured provider to
check connectivity and structured-output support. A loopback Ollama probe is
free; a hosted (`openai_compatible`) probe result explicitly says it may
incur real cost. Doctor never makes that call unless `--probe` is given, and
it never requires `apoapsis init` to run — a missing configuration is just
one more reported check.

Run the controlled `download-service` fixture through every execution lane
and get one comparison report:

```bash
apoapsis eval download-service
apoapsis eval download-service --lane local --lane one-shot
apoapsis eval download-service --lane forced-escalation --output-dir .apoapsis-eval/run-1
```

Each requested lane (`local`, `hybrid`, `forced-escalation`, `frontier`,
`one-shot`) runs against its own fresh, isolated copy of the fixture. `hybrid`,
`forced-escalation`, and `frontier` need `[models.frontier_coder]` configured;
without it, they are reported as skipped with a clear reason rather than
failing the whole command or making an unauthorized call. `forced-escalation`
proves a real local-to-frontier handoff by giving the local stage only a
one-turn budget, never by altering the task or the patch. Output is written to
`--output-dir` (default `.apoapsis-eval/<run-id>/`, already gitignored) as
`comparison.json` and `comparison.md`.

For `download-service`, the resumable acceptance oracle is removed before each
lane repository is initialized and is injected only after normal verification
has already declared completion. A normal pass followed by an oracle failure is
recorded as a false success; an oracle infrastructure error is not.

### The `local-strict` lane (opt-in)

`--lane local-strict` is a deliberately separate, opt-in lane measuring the
`STRICT` completion policy (ADR 0015/0016/0017) against a model-visible
acceptance check, not baseline completion:

```bash
apoapsis eval download-service --lane local-strict --output-dir .apoapsis-eval/strict-1
```

It is never part of the default lane set and every other lane keeps
selecting `BASELINE` explicitly regardless of your project's real
configuration, so historical false-success comparisons stay valid. The
`download-service` fixture ships a model-visible
`tests/test_resumable_visible_acceptance.py` (distinct data and test names
from the held-out oracle) — to use `local-strict` meaningfully, configure a
specifically named, acceptance-designated command for it in your own
`.apoapsis/config.toml` (acceptance designation is never generated
automatically, per ADR 0017):

```toml
[[verification.commands]]
name = "resumable-acceptance-check"
category = "acceptance"
description = "Model-visible resumable-download acceptance checks."
argv = ["python", "-m", "unittest", "tests.test_resumable_visible_acceptance", "-v"]
timeout_seconds = 60
required = false
acceptance = true
```

A model may then propose mapping an extracted acceptance criterion to
`resumable-acceptance-check` from the real catalog; nothing here injects or
rewrites that mapping — a missing or invalid one is a genuine result, not
hidden. Aggregate one or more persisted comparisons without making model
calls:

```bash
apoapsis eval-aggregate .apoapsis-eval/run-1/comparison.json \
  .apoapsis-eval/run-2/comparison.json \
  --output-dir .apoapsis-eval/aggregate
```

This writes `aggregate.json` and `aggregate.md` with completion, human-review,
unsafe-patch, false-success, latency, transmission, profile, and paired-lane
metrics. Hosted rescue and savings remain explicitly `unmeasured` unless the
loaded artifacts contain a paired real hosted-frontier run; fake providers test
the formulas but never populate real-world hosted results.

## Complete all-local agent flow

`apoapsis init` creates a 64K agent configuration for the installed Coder-Next Q4
model:

```toml
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 65536
think = false
specification_think = false

[models.frontier.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[models.local_coder]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
timeout_seconds = 900
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 65536
think = false

[models.local_coder.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0

[execution]
mode = "agent"
route = "auto"

[execution.agent]
max_turns = 12
max_patch_attempts = 4
max_verification_runs = 4
max_search_results = 20
max_read_lines = 240
max_observation_chars = 48000
max_transmitted_observation_chars = 24000

[execution.frontier_agent]
max_turns = 8
max_patch_attempts = 3
max_verification_runs = 3
max_search_results = 20
max_read_lines = 240
max_observation_chars = 48000
max_transmitted_observation_chars = 24000

[models.local_research]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3.6:27b"
timeout_seconds = 600
max_output_tokens = 8192
temperature = 0.0
context_window_tokens = 32768

[context]
max_files = 24
max_excerpt_lines = 240
max_total_chars = 180000
max_import_depth = 2
```

Both native Ollama endpoints must be loopback URLs. No fake API key is needed.
`models.frontier` remains the backwards-compatible specification/one-shot
provider; `models.local_coder` is the first agent stage. Then run one command:

```bash
apoapsis run "Add resumable downloads without changing the public API"
```

The generated default is the `64k` working profile. A run can select a reproducible
comparison profile without editing the project configuration:

| Profile | Ollama window | Files | Lines per excerpt | Total excerpt characters |
| --- | ---: | ---: | ---: | ---: |
| `16k` | 16,384 | 10 | 100 | 24,000 |
| `32k` | 32,768 | 16 | 160 | 72,000 |
| `64k` | 65,536 | 24 | 240 | 180,000 |
| `128k` | 131,072 | 32 | 320 | 360,000 |
| `256k` | 262,144 | 40 | 400 | 600,000 |

```bash
apoapsis run "Add resumable downloads without changing the public API" --context-profile 64k
```

`128k`/`256k` exist to be explicitly measured, not assumed safe because a
model or GPU happens to have the VRAM for them (ADR 0010) — `64k` remains
the default. `apoapsis doctor` checks a configured `context_window_tokens`
against the installed Ollama model's actually reported native context
length (`context_window_support:<role>`) before you rely on a wider
profile. Every model call also writes a `ContextMeasurement` (model window,
file/excerpt limits, transmitted chars, estimated tokens, window
utilization, composition, and stable-versus-newly-introduced evidence) as
its own audit artifact, surfaced on the task report and in `apoapsis eval`'s
comparison output — so a profile's actual effect is something you can read,
not guess.

The deterministic compiler also expands changed Python symbols to one-hop AST
call sites and related tests, and centers post-failure excerpts on validated
traceback locations. Agent observation history remains complete in
`agent-turn-*.json`, while only a current compacted view (24,000 characters by
default) is retransmitted. `context-attribution.json` reports the conservative
fraction of transmitted evidence whose file was actually changed by the
accepted patch. Prompt builders place a byte-stable instruction prefix first;
actual provider cache benefit is still reported only from token/cache telemetry.

Profiles affect the native local-coding window and deterministic retrieval;
Research Mode retains its separately configured budget. Apoapsis records the active
window and generation settings in every frontier request package and the exact
retrieval limits in every context package.

For sampling comparisons, Coder-Next temperature can be changed explicitly:

```toml
[models.frontier]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3-coder-next:q4_K_M"
temperature = 1.0
context_window_tokens = 65536
think = false
specification_think = false
```

Temperature is configurable for native Ollama and hosted providers and is
recorded in each request package. Zero remains the generated deterministic
sampling default; Coder-Next's published model settings recommend `1.0`.

Apoapsis displays the extracted Pydantic specification and waits for approval. The
`--yes` flag is available for controlled non-interactive evaluation. Approval
does not grant the model workflow authority: Apoapsis deterministically mediates
every search, read, patch, and configured check; records each request package;
and accepts completion only after full verification.

Use the retained one-shot baseline for a direct controlled comparison:

```bash
apoapsis run "Add resumable downloads without changing the public API" \
  --execution-mode one_shot --context-profile 64k
```

Agent mode is bounded by `[execution.agent]`. With no frontier coder configured,
an escalation request stops for human review. To enable automatic handoff, add
a separately authenticated provider:

```toml
[models.frontier_coder]
provider = "openai_compatible"
base_url = "https://provider.example/v1"
model = "frontier-coder"
api_key_env = "APOAPSIS_FRONTIER_CODER_API_KEY"
timeout_seconds = 900
max_output_tokens = 16384
temperature = 0.0

[models.frontier_coder.pricing]
input_per_million_usd = 0
output_per_million_usd = 0
cached_input_per_million_usd = 0
```

`route = "auto"` sends low, medium, and unclassified tasks local-first and
escalates only after the local stage stops. High-risk tasks go directly to the
frontier coder, while critical-risk tasks require human review. Routes can be
overridden with `--agent-route local_only`, `local_then_frontier`, or
`frontier_only`.

Before the first frontier coding call, Apoapsis writes
`frontier-escalation-package.json` containing the approved task and constraints,
the exact current diff, complete local action history, verification commands and
normalized failures, provider identities, and the frontier context digest. The
frontier agent continues in the same isolated worktree with its own deterministic
budget. If it cannot verify the task, Apoapsis stops for human review.

Every task writes `.apoapsis/tasks/<task-id>/report.json`. `apoapsis inspect <task-id>`
returns the persisted state/events and embeds that report when present.

The controlled download-service fixture and direct-versus-Apoapsis procedure are in
[`examples/download-service`](examples/download-service) and
[`docs/evaluation/direct-vs-apoapsis.md`](docs/evaluation/direct-vs-apoapsis.md). The first
measured local Qwen smoke results are in
[`docs/evaluation/local-qwen-smoke.md`](docs/evaluation/local-qwen-smoke.md).
The installed Coder-Next Q4 evaluation is in
[`docs/evaluation/qwen3-coder-next-smoke.md`](docs/evaluation/qwen3-coder-next-smoke.md).
The first bounded-agent run to complete the controlled fixture used ten agent
turns and three verification runs; all three tests passed with one source file
changed. The earlier one-shot failures remain documented as the comparison
baseline rather than being discarded. The first live `--lane local-strict`
evaluation (3 attempts, 0 completions, and a genuine harness gap it
surfaced) is in
[`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md`](docs/evaluation/apoapsis-strict-live-evaluation-2026-07-18.md).
A second round after fixing that gap (3 more attempts, 1 genuine
completion independently confirmed by the held-out oracle) is in
[`docs/evaluation/apoapsis-strict-live-evaluation-2026-07-19.md`](docs/evaluation/apoapsis-strict-live-evaluation-2026-07-19.md).

## Research Mode

Research runs only after specification approval. In `auto` mode, deterministic
rules activate it for research, precedent, product/UX, public API, CLI, report,
dashboard, and similar judgment-heavy work, while localized mechanical work is
skipped. Explicit modes are also available:

```bash
apoapsis run "Improve the task report UX" --research auto
apoapsis run "Add resumable downloads" --research github
apoapsis run "Why do users dislike coding-agent logs?" --research community
apoapsis run "Research and improve the onboarding report" --research full
```

Configure `[models.local_research]` in `.apoapsis/config.toml` with a locally
available Ollama model. GitHub and configured official documentation are enabled
by default. Reddit remains disabled until its approved API credentials and
applicable terms are configured.

Research can also be run independently for an already approved task:

```bash
apoapsis research TASK-ABC123 --mode full
apoapsis research inspect TASK-ABC123
apoapsis research refresh TASK-ABC123 --mode full
apoapsis research cache inspect
apoapsis research cache clear
```

The deterministic harness owns URLs, network access, budgets, provenance,
license classification, caching, and audit writes. The local model receives no
tools and may only propose structured questions, rankings, evidence, and
synthesis. Retrieved text is sanitized and marked `UNTRUSTED_EXTERNAL_CONTENT`.
The frontier model receives only the compact research brief and evidence IDs,
never full threads or fetched pages. External sources remain advisory: only the
approved task, repository policy, patch validation, and verification authorize
a change.

Research artifacts are written below
`.apoapsis/tasks/<task-id>/research/`; the final `report.json` includes the selected
mode, patterns, evidence IDs, local-model calls, tokens, latency, and whether the
brief influenced the proposed plan.

## Verification configuration

`apoapsis init` creates `.apoapsis/config.toml`. Commands are argument arrays, not shell
snippets:

```toml
[verification]
stop_on_failure = false
output_limit_chars = 100000

[[verification.commands]]
name = "unit-tests"
category = "tests"
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
```

By default (`backend = "host"`, implicit) commands run directly on the host —
deterministic, but not a security sandbox. Opt into the Docker-based sandbox
(ADR 0009) for network denial, CPU/memory/process limits, and a throwaway
worktree copy instead of the real one:

```toml
[verification.backend]
backend = "docker"

[verification.backend.docker]
image = "python:3.12-slim"
image_digest = "sha256:<pin this — see below>"
cpu_limit = 2.0
memory_limit_mb = 2048
pids_limit = 256
tmpfs_size_mb = 256
wall_clock_timeout_seconds = 300
```

Apoapsis never pulls an image automatically. Pull and pin one yourself, then
run `apoapsis doctor` to validate the whole preflight (CLI, engine, Linux
containers, image presence, a real minimal self-test) before relying on it:

```bash
docker pull python:3.12-slim
docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
apoapsis doctor
```

The Docker backend materially improves isolation but is not a defense
against container-runtime or kernel vulnerabilities; see ADR 0009's threat
model for exactly what it does and does not cover.

## Acceptance coverage and the completion policy (ADR 0015, 0016, 0017, 0018)

Configured verification passing is a development signal, not proof that the
product is done. `apoapsis init` writes `completion_policy = "strict"`, but
its generated command is **never** marked `acceptance = true`
automatically -- acceptance designation is always an explicit decision you
make once you have decided a command's pass is real product proof:

```toml
[[verification.commands]]
name = "unit-tests"
category = "tests"
description = "Runs the project's full test suite."
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 120
required = true
acceptance = false   # opt in explicitly once you decide this is real proof

[execution]
completion_policy = "strict"   # apoapsis init's default
```

Set `acceptance = true` on a command yourself when you're ready, then map
`AcceptanceCriterion.verification_method` to its name (or let a model
propose that mapping -- see below). Until you do, `apoapsis doctor` and the
UI overview both warn that `STRICT` has no acceptance-designated command,
and tasks with active acceptance criteria correctly stop at
`HUMAN_REVIEW_REQUIRED` instead of silently reaching `COMPLETE`.

Specification extraction receives a deterministic **acceptance-command
catalog** built fresh from `[verification.commands]` on every call (name,
category, `description`, and whether each is `acceptance_designated`); an
extracted `AcceptanceCriterion.verification_method` may name only a catalog
entry or stay `null` -- extraction rejects anything else, so a model can
propose a mapping but never invent one. The user still approves the whole
specification, mapping included, before it takes effect; the local UI's
specification view shows each criterion's proposed check.

Under `completion_policy = "strict"`, `COMPLETE` additionally requires
every active acceptance criterion to be computed as **Proven** -- mapped to
a command that is both configured and `acceptance = true`, and that has
actually **passed for the current worktree state**. "Current worktree
state" is a single shared fingerprint (ADR 0017): HEAD identity, the
canonical tracked diff, and every permitted untracked file's exact content
hash -- so a brand-new file a patch created without `git add`ing it (the
normal result of applying a diff) changes the fingerprint exactly like a
tracked edit does. A pass recorded before the worktree changed, tracked or
untracked, does not count: never-executed, failed, and passed are three
distinct, non-stale states, always scoped to the exact current fingerprint.
Unmapped, misconfigured, failing, or stale mappings stay
**Unproven**/**Failed** regardless of what a model claims; only the harness
computes and grants that status. A gap returns control to the bounded agent
with evidence (same budget, same loop) or, in one-shot mode, stops at
`HUMAN_REVIEW_REQUIRED` rather than spending its single repair attempt on
it. `inspect_diff` shows a model the same untracked-file state being
fingerprinted, as a bounded synthetic diff; untracked binary content and
symlink targets are never rendered, only a path-only placeholder, matching
existing binary/symlink policy elsewhere.

A failing acceptance-designated command always produces real, informative
failure evidence and an accurate turn summary (ADR 0018) -- even though it
is `required = false` and correctly never fails ordinary aggregate
verification or becomes a required development gate. Before this, a
failing optional acceptance command could be misreported as
`"deterministic verification passed"`, since the summary/evidence logic
only ever checked `required`.

If specification extraction's first response fails schema/Pydantic/
verbatim/catalog validation, the harness makes exactly one bounded
correction call containing the exact validation errors, the model's own
prior response, and the same schema/catalog/rules (ADR 0018) -- never a
second attempt, never coerced or nulled fields. If the correction also
fails, the task stops deterministically at `FAILED`.

`apoapsis eval` always explicitly selects `completion_policy = "baseline"`
for every lane, regardless of what a real project's configuration selects,
so false-success measurement against the held-out oracle stays comparable
across runs -- this is recorded on every persisted report and as a
"Completion Policy" column in the comparison Markdown, not silent
inheritance. The Pydantic configuration default (for code that builds a
config directly, bypassing `apoapsis init`) remains `"baseline"`.

## Repository layout

```text
src/apoapsis/
  agent/            bounded typed inspect-edit-test sessions
  cli/              CLI entry points
  context/          provenance-aware evidence schemas; deterministic context measurement
  execution/        managed Git worktrees; host/Docker execution backends
  models/           provider-neutral model request/response schemas
  repository/       deterministic Git inspection
  specification/    task and constraint schemas
  verification/     command runner and results
  workflow/         persisted state machine and events
  audit/            reproducible per-call and per-task artifacts
  patches/          unified-diff parsing, policy, and application
  reporting/        aggregate outcome and usage reports
  ui/               offline local operator interface and protected application API
  doctor.py         read-only environment/credential/provider preflight
  evaluation/       fixture-isolated lane runner and comparison report
tests/               deterministic unit and integration tests
docs/adr/            architectural decisions
HANDOFF.md           living architecture and project handoff
AGENTS.md            mandatory upkeep instructions for coding models
```

# Apoapsis next steps

This is the practical roadmap after the completed Apoapsis 1.0 implementation.
`HANDOFF.md` remains the canonical architecture and project-status record;
`AGENTS.md` remains mandatory instructions for coding models.

## For the owner

### 1. Use the new local-model controls

On Windows, double-click:

- `START_APOAPSIS.cmd` to start/check Ollama and warm the configured coding
  model for 30 minutes at its configured context size.
- `STOP_APOAPSIS.cmd` when finished. It explicitly unloads every configured
  local Ollama model, including the research model, and releases model RAM/VRAM.

The shared Ollama service stays running after Stop; it is lightweight and may be
used by other applications. Stop never touches hosted providers. To warm the
research model too, run:

```powershell
.\START_APOAPSIS.cmd --include-research
```

Loading both large models simultaneously may exceed available GPU/RAM even when
one model alone has context headroom. Leave Research Mode lazy unless it is
needed. Set `APOAPSIS_NO_PAUSE=1` when invoking either file from automation.

### 2. Collect the missing context-profile evidence

The framework is complete; the most valuable missing result is whether more
context actually helps this model on the same task. Start with 64k and 128k,
using the same task, model, quantization, generation settings, and lane:

```powershell
apoapsis eval download-service --lane local --context-profile 64k --output-dir .apoapsis-eval/profile-64k-1
apoapsis eval download-service --lane local --context-profile 128k --output-dir .apoapsis-eval/profile-128k-1
apoapsis eval-aggregate .apoapsis-eval/profile-64k-1/comparison.json .apoapsis-eval/profile-128k-1/comparison.json --output-dir .apoapsis-eval/profile-comparison-1
```

Repeat each profile at least three times before drawing a model-quality
conclusion. Compare completion and held-out-oracle results first; then context
tokens, attribution, cache telemetry, latency, and resource pressure. Do not
assume 256k is better merely because the model reports support for it.

### 3. Prove the sandbox success path

Docker's fail-closed path is live-proven; the success path still needs a pinned
local image and one explicitly authorized run. Follow ADR 0009 and the Docker
instructions in `HANDOFF.md`. Do not enable a silent host fallback.

### 4. Add hosted-frontier evidence only when desired

When real API credentials and pricing are configured, run paired identical
local-first and direct-frontier lanes. Preserve the complete comparison and
aggregate artifacts. Until then, hosted rescue and savings must remain
`unmeasured`; subscription access must not be represented as API access.

### 5. Use and review the first local application slice

Run `apoapsis ui` from an initialized project. The offline black/orange/purple
interface now shows real project, task, specification, event, report,
evaluation, and model-configuration data. Specification approval is live and
uses the same optimistic transition/event record as the CLI. Opening the UI does
not load or prompt a model; Doctor runs only when explicitly selected.

Natural-language model-assisted intake, execution orchestration, review/resume
choices, and native desktop packaging remain intentionally unavailable. Use the
CLI for those operations until the deterministic application services below are
built.

## For future coding agents

Read `AGENTS.md`, then all of `HANDOFF.md`, before making changes. Check the Git
status and preserve `substrate-v0.1` and all user work.

### Priority A — evidence before more retrieval machinery

1. Run repeated 64k/128k local evaluations on identical conditions.
2. Aggregate the persisted reports without model calls.
3. Record observed results in a new dated evaluation document.
4. Diagnose any quality difference from the audited context and action history.
5. Do not add embeddings, learned ranking, or model-selected context unless the
   deterministic lexical/symbol/import/test/diff path fails repeatably and the
   evidence identifies why.

Stop after publishing the evidence and ask for review before changing retrieval
architecture.

### Done — verification sufficiency and acceptance coverage (ADR 0015)

The 1.0 profile evidence above showed configured verification passing was not
proof of product correctness (4 of 5 completions had a failed held-out
oracle). This milestone added a real, product-level notion of "proven" that
composes with the existing bounded-agent/one-shot/escalation machinery
without touching retrieval, context compilation, or the held-out oracle:

- Three named verification layers (development, user-approved acceptance,
  held-out evaluation oracle) and a deterministic
  `AcceptanceCoverage`/`compute_acceptance_coverage()` record per criterion
  (`src/apoapsis/workflow/acceptance.py`).
- An opt-in `CompletionPolicy.STRICT` (default remains `BASELINE`, preserving
  today's held-out false-success comparability) that gates `COMPLETE` on
  every active acceptance criterion being Proven by a configured,
  user-approved acceptance-designated command -- never by a model's own
  claim.
- Ten deterministic fake-provider scenarios
  (`tests/test_acceptance_coverage.py`) covering unmapped/mapped/failing-
  then-passing criteria, a model's ineffective mapping attempt, two
  different valid tool sequences, multi-turn repair, fail-closed rejection
  of an unknown command, the oracle/workflow import-graph separation,
  baseline-unaffected behavior, and composition with frontier escalation.
- Report and UI surfacing of per-criterion status, configured agent budgets
  versus actual usage, and frontier availability/escalation state.

Not done, and the natural next evaluation once this is reviewed: map a real
`verification_method` onto the download-service fixture's acceptance
criteria and run `STRICT` against a real local/frontier model to see whether
it can productively repair toward the mapped acceptance command rather than
merely toward ordinary verification passing. Specification-extraction
reliability (the one 128k drafting failure noted above) remains a separate,
not-yet-investigated task.

### Priority B — review and resume experience

The highest-value product gap is a polished continuation path for tasks that end
in human review. Build this as deterministic control-plane functionality:

- list stopped tasks and their exact stop reason;
- show active constraints, current diff, policy findings, verification failures,
  budgets consumed, and remaining authorized options;
- allow explicit user choices such as retry with a configured remaining budget,
  authorize a configured frontier stage, abandon, or inspect only;
- append every decision to the existing audit/event record;
- never let a model choose a transition, command, retry ceiling, or completion.

Add an ADR before changing workflow/resume semantics. Cover each branch with a
fake provider and keep the one-shot baseline intact.

### Priority C — extend the accepted application shell (ADR 0014)

The first UI slice is complete: local/offline assets, a capability-protected
loopback API, real read-only task/report/environment/evaluation views, and
optimistic specification approval have deterministic integration and visual
coverage. CLI and UI approval produce the same persisted transition record.

Continue in this order:

1. Extract model-assisted task intake into a resumable application service. Do
   not keep a CLI input callback or HTTP request open while a model is running.
2. Persist a pending-approval operation and reconnect it to the existing
   specification extractor, provider telemetry, audit package, and exact
   verbatim-constraint checks.
3. Implement the explicit review/resume options from Priority B as typed service
   commands with optimistic versions and allowed-transition validation.
4. Add task execution progress through persisted events or a durable operation
   record; browser disconnects must not grant, cancel, or repeat authority.
5. Only then choose a packaged native wrapper for the proven loopback surface.

Keep `src/apoapsis/ui/application.py` as the authority boundary. Browser code
must not call providers, construct CLI commands, parse files into invented
state, or decide verification/completion.

### Priority D — operational proof and packaging

- Run the live-gated Docker success-path test with a pinned local image.
- Exercise `START_APOAPSIS.cmd` and `STOP_APOAPSIS.cmd` on the supported Windows
  setup; keep model endpoints loopback-only.
- Decide how a future native wrapper locates Python, Git, ripgrep, Ollama, and
  Docker without weakening `apoapsis doctor` or silently installing software.
- Add packaging only after resumable intake and review commands prove the
  application service; do not hide prerequisites or auto-download models/images.

## Always preserve these boundaries

- Models propose typed actions or patches; they never receive direct shell,
  filesystem, Git, network, workflow, retry, verification, completion, or audit
  authority.
- Verification and the held-out oracle are separate; oracle failures never
  become repair context.
- Hosted spend requires explicit provider configuration.
- Start/Stop manages only configured loopback Ollama models.
- No autonomous agent swarms, general-purpose work automation, or decorative UI
  that obscures whether a result was verified.

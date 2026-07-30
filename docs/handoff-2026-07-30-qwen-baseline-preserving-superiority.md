# Apoapsis handoff: preserve Qwen's baseline capability, then make it better

Date: 2026-07-30

Status: architecture and implementation assignment; not implemented

## Assignment

Redesign Apoapsis's local coding path so the harnessed Qwen is not a weaker
agent than the same Qwen running through its normal coding CLI.

The required product outcome is:

> Apoapsis must preserve the default Qwen coding agent's useful capabilities,
> add independent defect detection and repair, and ship a result that is
> measurably better than the default-Qwen control.

This supersedes the performance strategy in
`docs/handoff-2026-07-29-crisis-atlas-remediation.md`. That earlier handoff's
truthfulness, verification, integration, and operability fixes remain valid and
ADRs 0072-0076 remain implemented foundations. They are not sufficient to make
the local model perform well.

ADR 0071 remains decision history and its atomic change set remains a supported
fallback experiment. The unrestricted Crisis Atlas control is new evidence
that atomic multi-file JSON proposals alone do not restore the capability of a
normal coding CLI. Do not edit ADR 0071 retroactively. Record the new execution
boundary in a superseding ADR.

Read, in order:

1. `HANDOFF.md`;
2. `NEXT_STEPS.md`;
3. this handoff;
4. `docs/evaluation/crisis-atlas-64k-codex-frontier-trial-2026-07-30.md`;
5. `docs/evaluation/crisis-atlas-qwen-cli-control-2026-07-30.md`;
6. ADRs 0059, 0069-0076; and
7. `docs/product-design-handoff.md` for application-facing work.

Preserve uncommitted owner work and the `substrate-v0.1` tag.

## Non-negotiable success definition

No honest evaluation can prove that one stochastic model run will beat every
other possible run. Apoapsis can, however, make non-regression an enforced
architecture and release property rather than an aspiration.

The new local path must meet four separate gates:

### 1. Capability preservation

For the same model, weights, quantization, server options, context size, output
cap, seed, task, repository, sandbox image, and wall-clock budget, the Qwen
inside Apoapsis must have the same useful coding interface as the default Qwen
CLI control:

- persistent shell;
- repository-wide file inspection and search;
- ordinary file editing;
- arbitrary local development commands inside the sandbox;
- self-directed test and debug loops;
- multi-file changes without serializing whole files into one JSON response;
- persistent working-directory state; and
- context continuation or compaction.

An Apoapsis-specific action grammar may be offered as a compatibility path, but
it must not be the primary high-capability path.

### 2. Proposal non-inferiority

Before any stronger-model or human repair, the harnessed arm must not score
below the matched default-Qwen arm on independently measured implementation
quality. Every benchmark case that the default arm passes must also pass in the
harnessed arm before the new mode may become the default.

### 3. Delivered-result superiority

After Apoapsis verification and any explicitly authorized repair, the accepted
result must:

- pass everything the matched default-Qwen result passes;
- have no additional independently observed regressions;
- catch at least one defect or unmet obligation that the default agent's own
  completion claim missed across the release corpus; and
- never convert missing evidence into `COMPLETE`.

### 4. Efficiency

Quality has priority over token savings, but the architecture must also remove
the unrestricted control's avoidable prompt replay. Before default rollout:

- median total input tokens across the paired corpus must be lower than the
  default-Qwen control;
- context- or output-ceiling failures must be reported separately from model
  reasoning failures;
- median provider latency must be reported, not hidden inside total wall time;
  and
- any quality gain purchased with additional frontier calls must be itemized.

Do not combine these four gates into one average that lets a cheap failure
cancel out a quality regression.

## What the Crisis Atlas evidence actually says

### Matched outcomes

| Arm | Qwen calls | Input tokens | Output tokens | Result before independent review |
| --- | ---: | ---: | ---: | --- |
| 64K sliced Local Power | 19 | 258,632 | 55,364 | Slice 2 falsely completed after a partial wrong-path service; Slices 3-4 needed repair |
| Unrestricted Qwen CLI control | 62 successful, 63 attempted | 2,080,801 | 35,787 | Coherent whole product, 88 self-authored tests passing, but a broken status filter and strict-web warnings |
| Sliced Qwen plus Codex checkpoints | 19 Qwen calls plus Codex repair | 258,632 Qwen input | 55,364 Qwen output | Verified product candidate passed the configured gates and browser lifecycle |

The unrestricted control used about eight times the Qwen input tokens, so it
was not more token-efficient. It was substantially better at proposing a
coherent product. Apoapsis plus a genuinely stronger reviewer produced the best
final result, but the repair checkpoints were not authoritative plan
checkpoints and therefore did not remain fully inside the state machine.

### Slice 2 is direct evidence of a harness-caused capability regression

The Slice 2 task asked for both `IncidentService` and `ExportService` plus
tests. Its first Qwen response said it would implement both, but proposed only
one partial file at `services/incident_service.py`, the wrong package path. It
created no export service and no tests.

The response was short, so this was not output truncation. The prompt contained
relevant repository evidence, so this was not a missing-context proof either.
Qwen made a poor first proposal.

The decisive failure happened next: Apoapsis applied that one change, ran
inherited checks, observed green, and terminated the session as `COMPLETE`.
Qwen did not independently decide that the slice was done and did not receive
another turn in which it could inspect its work, notice the omissions, or
implement the rest of its own stated plan.

The exact lesson is:

> The model made the incomplete edit; the harness converted an incomplete
> first edit into a final result.

This is why the harnessed Qwen was worse even though it had tests and a clean
environment.

### Why “tests plus a nice environment” were not enough

1. **The tests were inherited, not slice-mapped proof.** They stayed green
   because they never imported or exercised the new service file.
2. **Verification doubled as premature termination.** ADR 0069 correctly
   stopped redundant verification loops, but “all configured checks are
   green” was weaker than “the active slice is implemented.” The optimization
   ended productive model work.
3. **The interface taxed the model.** The normal agent could inspect, edit,
   run commands, and repair in a persistent shell. Local Power required one
   typed JSON action per model call and often required entire file contents in
   the response. ADR 0071 improved multi-file granularity but retained the
   artificial envelope and harness-driven stop.
4. **The prompt supplied volume instead of navigation.** Slice prompts
   front-loaded large excerpts, inherited tests, contracts, and replayed
   history. The default agent chose what to inspect as it worked. More supplied
   context did not mean more usable attention.
5. **The budget described protocol turns, not engineering work.** Twelve
   Local Power turns were not equivalent to the unrestricted control's 62
   successful model/tool cycles.
6. **Slice continuity lacked a compact architectural memory.** Later slices
   inherited code but not a small authoritative interface and obligation
   ledger optimized for model use.
7. **The “frontier” continuation was sometimes the same Qwen endpoint.** More
   turns from the same model are not an independent capability tier.
8. **Self-authored tests were treated too generously.** The unrestricted agent
   wrote 87 additional tests and still missed the broken filter. Tests helped
   it build; they did not independently prove the product.

Slices 2 and 3 therefore contain both model gaps and harness gaps:

- Qwen owns the wrong path, missing components, and ordinary server bugs.
- Apoapsis owns the false completion, weak witnesses, missing new-component
  coverage, and failure to give the model a baseline-capable repair loop.

## Research basis

This redesign is consistent with primary-source work on coding agents:

- The [SWE-agent paper](https://arxiv.org/abs/2405.15793) found that an
  agent-computer interface materially changes the same model's ability to
  navigate repositories, edit files, and execute tests. Its
  [ACI design notes](https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md)
  report that a baseline without a tuned interface performs much worse and
  emphasize concise, useful tool observations.
- The [OpenHands paper](https://arxiv.org/abs/2407.16741) combines developer-like
  code and command-line interaction with safe sandboxed execution. Capability
  and containment are separate layers.
- [Qwen Code's own tool documentation](https://qwenlm.github.io/qwen-code-docs/en/developers/tools/introduction/)
  defines filesystem tools and `run_shell_command` as core capabilities and
  describes applying sandbox restrictions around those tools.
- Anthropic's
  [context-engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  describes context pollution, just-in-time repository exploration,
  compaction, and persistent structured notes for long-horizon agents.
- OpenAI's
  [computer-environment architecture](https://openai.com/index/equip-responses-api-computer-environment/)
  similarly uses a persistent shell/container, bounded command output,
  on-demand file access, and compaction instead of packing every artifact into
  the prompt.

These sources do not prove this particular implementation. They do support the
design premise that a safe agent does not need to be a cognitively crippled
agent.

## Superseding architecture: baseline agent inside, authority outside

The new architecture should be called the **Capability Sandbox** until an ADR
chooses a permanent name.

### The key boundary change

The old rule says the model may not have direct shell or filesystem authority.
That rule conflates two different kinds of authority:

1. ephemeral capability inside a disposable environment; and
2. durable authority over the owner's repository, network, credentials,
   workflow, evidence, and delivery.

The new boundary should allow the first and continue to deny the second:

> Qwen may act like a normal coding agent inside a disposable workcell.
> Apoapsis alone decides whether any resulting delta is valid, verified,
> checkpointed, promoted, or delivered.

This is an architectural change and requires an explicit ADR before
implementation. It does not authorize a model shell on the Windows host.

### Layer 1: the inner Qwen workcell

Run the default Qwen coding CLI, or a conformance-tested equivalent using the
same native tool loop, inside a disposable container/worktree.

The workcell should provide:

- a writable disposable clone including normal Git inspection commands;
- a persistent shell and working directory;
- standard repository search, read, edit, and local test tools;
- batching and ordinary shell composition;
- a writable temporary directory and bounded local process execution;
- the approved plan/task as a read-only mounted artifact outside the delivered
  project tree; and
- a single `ready_for_evaluation` or checkpoint signal.

The checkpoint signal is a request for inspection, not a completion decision.
Qwen cannot set task state, approve a plan, promote a commit, or create a
delivery.

Do not require source files, diffs, shell commands, or reasoning to be embedded
in a bespoke one-action JSON envelope if the provider's native tool calling is
working. Retain the current typed-action Local Power implementation as a
lower-capability compatibility mode.

### Layer 2: hard containment around the workcell

The container controller, not the prompt, enforces:

- no host filesystem mounts except the disposable project clone and explicit
  read-only task artifacts;
- no Docker/Podman/WSL control socket;
- no owner credentials, SSH agent, cloud metadata, browser profile, or package
  tokens;
- network disabled by default, with future egress allowlisting owned by the
  controller;
- non-root execution;
- process, memory, CPU, disk, output, and wall-clock ceilings;
- a bounded process tree that is killed on stop;
- immutable capture of model requests, tool calls, command output, and final
  worktree fingerprint; and
- a disposable filesystem that can be destroyed without affecting the source
  repository.

The model may use Git locally because the clone is sacrificial. Remove or
sanitize remotes and provide no network credentials. Apoapsis must compute the
authoritative final delta and ancestry outside the model's trust domain.

### Layer 3: policy and delta admission

At a checkpoint, Apoapsis:

1. freezes the workcell;
2. computes the complete delta against the approved base;
3. classifies changed, added, deleted, generated, test, dependency, and
   forbidden paths;
4. applies patch-policy and size ceilings to the whole delta;
5. records all findings at once;
6. rejects or snapshots the candidate atomically; and
7. runs verification in a separate clean verifier workcell.

This preserves the useful atomicity of ADR 0071 without forcing Qwen to express
its engineering work as an atomic JSON payload.

Forbidden changes never reach the owner branch. A policy rejection should
return a compact list of all violations to a fresh or resumed repair context.

### Layer 4: independent verifier

Verification must run in a clean controller-owned environment created from the
approved base plus the admitted candidate delta. It must not trust:

- Qwen's claim that tests passed;
- Qwen-authored test names;
- a configured command's friendly label;
- artifacts generated in the coding workcell; or
- a server Qwen left running.

The verifier owns process cleanup, command invocation, evidence capture, and
worktree fingerprint binding. Existing ADR 0074 final integrated verification
and ADR 0076 operability checks remain part of this layer.

### Layer 5: repair and promotion

If verification fails:

1. return a compact failure packet to Qwen while budget remains;
2. require another explicit checkpoint;
3. escalate only under the approved routing policy; and
4. if frontier review is authorized, use a genuinely stronger model, not the
   same Qwen endpoint with more turns.

A Codex or human repair must become an authoritative `PlanCheckpoint`:

- exact parent checkpoint;
- admitted delta;
- resulting commit and fingerprint;
- actor class (`local_model`, `frontier_model`, or `human`);
- commands and structured witnesses;
- acceptance obligations proved and still open; and
- the state transition it authorizes.

Later slices and delivery must inherit that checkpoint through the state
machine. Direct repair commits outside the plan graph are evaluation evidence,
not a deliverable plan.

## Preserve the baseline prompt and improve context around it

The baseline-preserving mode should begin from the same concise task prompt and
tool schemas as the default-Qwen control. Apoapsis-specific material belongs in
small, stable artifacts the model can inspect on demand.

### Stable task kernel

Provide a compact read-only task document containing:

- objective;
- active slice;
- acceptance obligations;
- architecture and integration contracts;
- protected/forbidden operations;
- canonical commands available for self-testing;
- checkpoint instructions; and
- the distinction between “checkpoint requested” and “accepted complete.”

Do not resend the complete task, all excerpts, all tests, and the full
transcript on every turn.

### Persistent state capsule

Across compaction or continuation, preserve:

- current objective and slice obligations;
- architecture/interface ledger;
- changed-path and delta summary;
- worktree fingerprint;
- tests and witnesses already observed;
- latest failures;
- unresolved acceptance obligations;
- refused or no-progress actions; and
- Qwen's concise working notes, clearly marked advisory.

Keep recent tool calls verbatim. Drop old raw terminal logs after their
important facts are represented in the capsule. Repository files remain on
disk and should be retrieved just in time.

### Context and output ceilings

Add first-class stop reasons:

- `INPUT_CONTEXT_PRESSURE`;
- `INPUT_CONTEXT_EXHAUSTED`;
- `OUTPUT_CEILING_TRUNCATION`;
- `TOOL_OUTPUT_TRUNCATION`; and
- `PROVIDER_ERROR_AFTER_ROLLOVER`.

Start compaction before the hard context ceiling, using a configurable,
measured threshold. Preserve the state capsule and most recently accessed
files. Never treat a response ending at the output cap as ordinary model
failure or valid JSON.

The Crisis Atlas evidence supports keeping a 16K-capable output profile for
coherent multi-file work. It does not support changing the default input
context solely because 64K fits in memory.

## Performance optimizations that belong in the design

Capability and correctness come first, but the second research pass identified
additional ways to make the supervised agent faster and more token-efficient
without narrowing what it can do.

### Use Qwen Code's native loop instead of rebuilding a slower imitation

Pin and record an actual Qwen Code/SDK version for the baseline and Capability
Sandbox. Prefer its structured headless event stream over scraping terminal
text. Qwen Code already provides:

- persistent foreground and background shell processes;
- explicit process IDs and exit information;
- session resume;
- file, glob, and ripgrep-backed search tools;
- context compaction;
- bounded tool output;
- output-cap recovery;
- checkpoint/restore support; and
- optional LSP navigation and diagnostics.

The official
[headless-mode documentation](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/)
defines line-delimited `stream-json` events, session identity, usage, and
headless resume. The
[shell documentation](https://qwenlm.github.io/qwen-code-docs/en/developers/tools/shell/)
distinguishes foreground tests from persistent background servers and returns
stdout, stderr, exit code, signals, and background PIDs.

Apoapsis should adapt those events into its audit log and checkpoint protocol,
not insert a second model-action scheduler in front of them.

### Prove provider and tool-template conformance first

A capable CLI can still perform badly if the OpenAI-compatible adapter,
`llama-server` chat template, or tool parser subtly disagrees with Qwen Code.
Before measuring agent quality, run a conformance suite that proves:

- system, user, assistant, tool-call, and tool-result roles round-trip in the
  intended Qwen chat template;
- single and parallel tool calls preserve names and JSON arguments;
- multiline Unicode file content is not escaped, truncated, or double-encoded;
- thinking blocks are either supported or stripped exactly once;
- stop reasons distinguish normal completion, tool call, context limit, output
  limit, cancellation, and provider error;
- usage counts and maximum-output settings are retained;
- a replayed or retried response cannot execute the same mutating tool twice;
  and
- Qwen Code's declared context/output limits match the actual server profile.

Pin the chat template, tool schemas, CLI version, and server version in every
evaluation manifest. A malformed tool envelope is an adapter defect until the
conformance suite proves otherwise.

### Give Qwen efficient edit choices

Do not force every modification to resend a whole file. Preserve the normal
Qwen tool portfolio:

- whole-file create/write for new or heavily rewritten files;
- exact replacement for small local changes;
- patch application for coherent multi-hunk edits;
- rename/delete;
- formatter or code action where repository policy permits; and
- checkpoint restore inside the disposable workcell.

Every edit should return a concise changed-range summary and immediate syntax
status. Apoapsis still evaluates the complete final delta at admission.

This reduces output tokens while avoiding the old unified-diff-only failure
mode: Qwen chooses the representation suited to the change, and a malformed
patch does not consume the whole task.

### Prebuild the environment and reuse only safe caches

Build versioned workcell/verifier images containing the approved language
runtimes, browsers, linters, language servers, and test tools for the benchmark
family. Avoid spending agent time discovering that a basic tool is absent or
reinstalling it on every slice.

Where lockfiles permit it, mount package/download caches read-only or through a
controller-owned cache proxy. Bind their digest into evidence. Never share:

- writable virtual environments;
- build outputs;
- databases;
- background processes;
- unreviewed generated source; or
- dependency state not reproducible from the admitted manifest/lockfile.

Snapshot a prepared dependency layer after owner-approved installation, then
start coding and verification workcells from that same layer. This makes the
two environments fast and comparable without letting the coding agent's
leftovers become verifier evidence.

### Keep observations small, lossless, and recoverable

Use different observation budgets by tool:

- search/glob: path plus compact match summary;
- file read: requested line window with explicit continuation coordinates;
- test failure: failing test names, first causal traceback, and short
  stdout/stderr head and tail;
- successful test: command, counts, duration, and artifact pointer;
- long shell output: bounded head and tail in context, full output written to
  an immutable artifact the model can inspect on demand; and
- background server: readiness/status deltas, not the whole accumulated log.

Qwen Code exposes configurable tool-output truncation and uses ripgrep by
default. Its current configuration defaults to a 25,000-character tool-output
threshold, while recent Qwen Code releases spill oversized output to files
instead of repeatedly filling context. SWE-agent's ACI work likewise found
that concise search results and small file windows were less confusing than
showing every match.

Truncation must be visible and reversible. Never silently discard the only
error line.

### Preserve prompt-cache locality

Arrange every model request as:

1. stable system prompt;
2. stable, deterministically sorted tool schemas;
3. stable task kernel;
4. compacted history/state capsule; and
5. latest observation.

Do not inject timestamps, random IDs, reordered schemas, or changing audit
metadata into the stable prefix. Keep those in tool results or suffix metadata.

Qwen Code documents automatic
[token caching](https://qwenlm.github.io/qwen-code-docs/en/users/features/token-caching/)
for compatible OpenAI-style providers. `llama-server` also documents slot
prompt-cache checkpoints, host-RAM cache, and idle-slot caching in its
[server reference](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
Measure both rather than assuming the hosted-provider cache behavior applies to
the local endpoint.

For a single local agent:

- pin the session to one server slot when practical, because cache reuse may be
  slot-local;
- keep one warmed model service for all calls in a controlled arm;
- issue one readiness call before the timed first engineering call;
- report cold-load time separately from prompt evaluation and generation; and
- do not run unrelated inference through the same slot during a measurement.

### Compact before the cliff, with two tiers

Use a cheap first tier that removes old reasoning blocks and replaces old tool
outputs with artifact pointers. Use semantic compaction only when the remaining
history still approaches the threshold.

Qwen Code's current default auto-compaction threshold is 70% and it provides
both semantic `/compress` and rule-based `/compress-fast`. Treat 70% as the
first experiment point, not an unquestioned Apoapsis constant.

Compare at least:

- 60%, 70%, and 80% thresholds;
- fast cleanup only versus fast cleanup followed by semantic compaction;
- state-capsule recall after rollover; and
- tokens saved, prompt-evaluation latency, and post-compaction defect rate.

The winning threshold must preserve architecture decisions, open obligations,
and failure evidence on the benchmark corpus.

### Add immediate diagnostics without adding premature completion

Provide Qwen with low-latency feedback after edits:

- syntax/parser validation;
- language-server diagnostics and symbol/reference navigation when the project
  already has a safe configured language server;
- changed-file formatter/linter checks;
- targeted tests selected from repository facts; and
- import/build checks for newly added components.

Qwen Code's
[LSP documentation](https://qwenlm.github.io/qwen-code-docs/en/users/features/lsp/)
supports definitions, references, call hierarchy, workspace symbols,
diagnostics, and code actions. SWE-agent reports that immediate syntax linting
at edit time was especially useful.

These are model feedback, not acceptance verdicts. They should help Qwen catch
Slice 3-style route and type mistakes sooner while the independent verifier
retains completion authority.

### Provide localhost-only product diagnostics

For browser products, include an offline headless browser in the workcell and
let Qwen exercise only the workcell's own loopback origin. Block external
requests at the container boundary and use a fresh profile with no owner
cookies, extensions, credentials, or browser history.

Useful model-facing observations are:

- DOM/accessibility-tree excerpts;
- console errors;
- failed local network requests;
- rendered viewport dimensions;
- focus/label/accessibility findings;
- screenshot artifacts where the configured model supports images; and
- exact steps from an owner-authored local scenario.

The independent verifier repeats owner-approved browser scenarios in a clean
profile and emits the authoritative structured witness. Model-driven browsing
is diagnostic work inside the sacrificial environment, not acceptance or host
browser authority.

### Use an adaptive verification pyramid

Do not run the slowest full suite after every edit:

1. Qwen runs focused diagnostics and tests while developing.
2. At checkpoint, Apoapsis runs changed-component and slice-mapped witnesses.
3. If those pass, it runs required slice acceptance.
4. At authoritative checkpoint/promotion, it runs the configured broader
   regression set.
5. At plan delivery, ADR 0074 final integrated verification runs once against
   the exact final candidate.

Cache a passing result only for the exact command definition, environment
digest, worktree fingerprint, and dependency state. A changed production file,
test, dependency, configuration, or witness wrapper invalidates the relevant
cache.

This reduces wall time without bringing back ADR 0069's false-completion error.

### Let read-only exploration overlap; serialize mutations

Allow Qwen Code's native parallel read/search calls and background server/test
monitoring when the underlying tools declare themselves read-only or
process-isolated. Serialize:

- file writes;
- dependency installation;
- database mutation;
- Git operations that alter state; and
- checkpoint freeze/admission.

Optional subagents should begin as read-only reconnaissance or independent
review experiments with separate contexts and explicit token accounting.
Qwen Code warns that forked agents share a worktree and concurrent edits may
conflict. Do not enable parallel implementation agents until isolated
worktrees, merge admission, and a paired quality gain are demonstrated.

### Route reasoning effort by task difficulty

The Crisis Atlas arms ran with reasoning disabled. Qwen's official model
documentation describes thinking mode as a controllable quality/compute tradeoff
for harder coding and reasoning tasks, and Qwen Code exposes provider-mapped
reasoning effort.

Add a controlled profile experiment:

- non-thinking for search, mechanical edits, and routine repair;
- medium/high thinking for architecture reconciliation, cross-slice
  integration, unfamiliar failures, and pre-checkpoint self-review; and
- no silent switch of model, prompt, or effort inside a paired arm.

Record reasoning tokens, tool-call validity, first-checkpoint quality, latency,
and total repair distance. Reasoning mode becomes a default only if it improves
independent quality per compute on this exact local model/runtime; it is not
assumed from another Qwen release.

Also test one read-only fresh-context local review at checkpoint. It is still a
Qwen continuation, not frontier review, but a clean context focused on the
admitted diff and open obligations may catch omissions cheaply. Keep it only if
it reduces downstream repair tokens or defects on held-out cases.

### Adapt output capacity instead of always reserving the maximum

Start ordinary tool-selection turns with a normal output allowance. Before a
coherent code-generating response, permit a higher declared cap. If the
provider reports `length`/`MAX_TOKENS`, retry from the same checkpoint with a
higher cap or ask the agent to use file tools rather than regenerating a giant
payload.

Qwen Code now documents adaptive output-token escalation because truncated
file-tool arguments are unusable. Apoapsis should classify and recover from the
condition, while retaining the owner's hard maximum.

### Tune `llama-server` only behind quality-preserving benchmarks

Create an owner-specific server profile benchmark for:

- GPU layer offload;
- CPU generation and prompt-processing threads;
- logical and physical batch sizes;
- Flash Attention;
- KV-cache precision and memory use;
- one slot versus controlled parallel slots;
- context-checkpoint/host-RAM prompt cache;
- memory mapping/locking where supported; and
- optional speculative decoding.

The official `llama-server` reference exposes these controls. Optimize for
prompt-evaluation tokens/second, generation tokens/second, time to first token,
and stable long-context operation. Reject a faster profile if it changes tool
validity, acceptance score, determinism, or long-context recall.

Do not casually quantize the KV cache or add a draft model merely because it
fits. Those are separate experimental arms with identical output-quality gates.

### Keep the model resident, but clean task state

For a multi-slice plan, reuse the warm model process and immutable tool/runtime
image. Start each slice from:

- the authoritative prior `PlanCheckpoint`;
- a fresh coding workcell filesystem built from that checkpoint;
- the compact architecture/obligation ledger; and
- no stale background processes, ports, environment mutations, or unreviewed
  conversation debris from the previous slice.

This preserves model-load and prompt-cache advantages without allowing process
state to become hidden evidence.

### Measure performance as a decomposition

Per call and per task, record:

- model load/readiness;
- queued time;
- prompt-evaluation tokens and milliseconds;
- cached/reused input tokens where the provider exposes them;
- generation tokens and milliseconds;
- time to first token;
- tool execution time;
- verifier time;
- compaction time and tokens;
- repair/escalation time;
- peak host RAM, VRAM, and KV-cache allocation; and
- work completed at first checkpoint.

Report token and latency savings only after final-quality parity. A fast false
completion is a regression.

### Performance experiment order

Change one variable at a time, in this order:

1. baseline-native Qwen loop versus legacy Local Power;
2. readiness-based completion versus green-test termination;
3. bounded tool observations and artifact spill;
4. fast/semantic compaction thresholds;
5. stable-prefix and local prompt-cache reuse;
6. edit-tool portfolio and provider/tool conformance;
7. LSP, localhost-browser, and immediate diagnostics;
8. adaptive verification and prepared environment layers;
9. reasoning effort and clean-context local review;
10. read-only parallelism; and
11. server/KV/speculative-decoding tuning.

This order prevents a throughput optimization from hiding a capability or
acceptance regression.

## Replace green-test completion with slice readiness

Required commands are necessary evidence. They are not the definition of a
completed slice.

Compile every approved slice into a `SliceAcceptanceContract` before model
spend. It should contain:

- every active criterion;
- required production artifacts and interfaces;
- required test or witness obligations;
- integration edges introduced or consumed by the slice;
- documentation and operability obligations;
- negative controls;
- independent versus model-authored evidence classification; and
- explicit reasons for anything intentionally unmeasured.

The harness may accept a checkpoint only when every required obligation is
either proved by current-state evidence or explicitly routed to human review.

### New-component rule

A new production component cannot complete solely because inherited tests
remain green.

At least one current-state witness must prove the new path is reached. This can
be:

- a new or updated independent test;
- an existing test whose structured coverage witness proves it imported and
  exercised the new component;
- a behavioral witness that invokes the component through the product
  boundary; or
- an explicit owner-approved reason that the component is intentionally
  unmeasured, which prevents automatic `COMPLETE`.

Merely adding a test file is not enough, and merely preserving inherited green
tests is not enough.

### Structured witnesses

Owner-approved verification commands should emit or be wrapped into a
versioned JSON witness. For a launch/API test, the witness should record:

- command identity and version;
- candidate commit and worktree fingerprint;
- process launched and readiness condition;
- actual bound address;
- routes exercised;
- request methods and normalized response assertions;
- mutations and subsequent reads;
- restart/reload behavior where required;
- cleanup result;
- artifact hashes; and
- criteria proved.

A command named `behavioral-integration` is not evidence that integration
occurred. The structured witness is.

The wrapper must fail closed when it cannot produce the declared evidence. Keep
raw output as audit detail, not as the only machine-readable proof.

### Verification scheduling

Qwen may run local diagnostics whenever it chooses inside the workcell.
Apoapsis runs authoritative verification:

- when Qwen requests a checkpoint;
- after an admitted repair;
- at the final integrated-project gate; and
- when an owner explicitly requests it.

Do not automatically terminate after the first admitted edit merely because
legacy required commands pass. `verify_after_change_set` may remain a
diagnostic option, but a pass cannot end the session unless the
`SliceAcceptanceContract` is also ready.

## Make independent review genuinely independent

The local and frontier roles must name different capability tiers in
configuration and audit evidence.

Minimum requirements:

- local role: Qwen endpoint and exact model fingerprint;
- frontier role: explicitly configured stronger model/provider fingerprint;
- no silent fallback from frontier to local;
- if the stronger model is unavailable, stop for human review;
- frontier receives the admitted delta, state capsule, failing witnesses, and
  open obligations rather than the entire noisy local transcript;
- frontier may propose a repair but cannot waive verification; and
- every repaired checkpoint is re-run through the same verifier.

Using the same Qwen endpoint with a larger turn budget is a continuation, not
frontier review.

## Two scorecards, never one

Every evaluation and task report should show:

### Model proposal quality

- active obligations implemented before external repair;
- independent checks passed at first checkpoint;
- missing, wrong-path, placeholder, or dead production artifacts;
- runtime defects found;
- repair distance in files and changed lines;
- model-authored test relevance;
- context/output truncations; and
- calls, tokens, latency, and elapsed time to first coherent checkpoint.

### Harness defect-detection quality

- seeded and naturally occurring defects detected;
- false-complete count;
- criteria with mapped current-state evidence;
- structured witness coverage;
- weak command-name-only claims refused;
- negative controls caught;
- stale or inherited evidence rejected;
- integrated defects caught before delivery; and
- defects found independently after Apoapsis accepted the result.

Also report final delivered quality, but do not use a strong frontier repair to
rewrite the local model's proposal score.

## Architectural negative controls

Maintain small deliberate mutations tied to product invariants. At minimum,
the Crisis Atlas family must include:

- replace API persistence with `localStorage`;
- discard URL query parameters while leaving the route reachable;
- return static/sample incidents instead of backend state;
- make `/` return 404 while an internal API path still works;
- shadow a specific export route with a broad route;
- make restart persistence fail;
- make JSON or Markdown export nondeterministic;
- remove the only test/witness reaching a newly added component;
- leave an inaccessible form control or blocking alert in the UI; and
- terminate a model response exactly at the configured output ceiling.

Negative controls are evaluation fixtures. Keep their expected outcomes hidden
from the repair model. Record which layer caught each mutation.

## Baseline-preserving evaluation

### Arms

Use fresh clones of the same seed for each paired run:

1. **Default-Qwen control:** the normal Qwen coding CLI in the same hardened
   workcell, with the approved task/handoff and no Apoapsis action protocol.
2. **Capability Sandbox:** the same inner Qwen CLI and prompt, supervised by
   Apoapsis admission, acceptance, compaction, and repair.
3. **Legacy Local Power:** optional diagnostic arm with ADR 0071 atomic change
   sets; never use it as the baseline.

The control must be safe too. “Default” describes the agent interface, not host
access.

### Controlled variables

Bind and record:

- seed commit and worktree fingerprint;
- task and plan object hashes;
- model file hash, quantization, endpoint, sampling seed, and server flags;
- context and output caps;
- system and user prompt hashes;
- tool/CLI version and container image digest;
- CPU/GPU allocation;
- wall-clock and process ceilings;
- network and mount policy; and
- independent verifier version.

### Corpus and repetitions

Do not promote the new mode from one Crisis Atlas run. Use:

- Crisis Atlas;
- Focus Orbit;
- a small backend-only change;
- a cross-file refactor;
- a test-repair task;
- a launch/operability task;
- a task with a deliberately misleading inherited suite; and
- at least one repository the model has not seen during prompt tuning.

Run at least three deterministic seeds or repeated samples per task where the
provider permits it. Report per-case results as well as aggregates.

### Release gates

The Capability Sandbox may become the recommended local mode only if:

1. every case passed by matched default Qwen is also passed by the harnessed
   arm before frontier repair;
2. aggregate proposal quality is no lower;
3. final independently verified quality is higher on at least one case and no
   lower on any case;
4. false-complete rate is lower and no accepted defect escapes the held-out
   checks;
5. every required criterion has mapped current-state evidence;
6. all negative controls are caught by the expected layer;
7. median input tokens are below the default-Qwen arm;
8. context and output truncations are correctly classified;
9. every frontier/human repair is represented as an authoritative plan
   checkpoint; and
10. the complete deterministic harness suite has no new failures;
11. prompt-evaluation, generation, tool, verification, and compaction time are
    reported separately;
12. any reasoning, caching, LSP, parallelism, KV-cache, or speculative-decoding
    optimization passes the same per-case quality floor; and
13. the selected profile is tested both cold and warm.

If a matched case regresses, keep the mode experimental. Do not explain away a
regression with a better average.

For high-assurance use before the corpus is mature, support an optional
`paired_parity_guard`: run the default and supervised arms from the same base,
verify both independently, and allow the stronger reviewer to choose or repair
the better admitted candidate. This costs more but provides an observed
per-task fallback instead of relying only on aggregate confidence.

## Crisis Atlas must-pass regression

The new architecture does not pass its first flagship test unless a fresh run:

1. implements every slice artifact in its declared package;
2. refuses Slice 2 after only the partial wrong-path service;
3. proves both service classes through current-state evidence;
4. starts the canonical server and exercises the actual routes;
5. uses the HTTP API from the dashboard;
6. creates an incident whose state survives reload and server restart;
7. correctly filters status through both API and browser UI;
8. round-trips status, timeline events, and action items;
9. produces deterministic JSON and Markdown exports;
10. passes the strict web-product check or records only reviewed, explained
    dynamic-analysis limitations;
11. catches the `localStorage` and discarded-query negative controls;
12. passes final integrated verification;
13. records any Codex/human repair as an authoritative plan checkpoint;
14. keeps Report, task state, final verification, and `delivery.json`
    consistent; and
15. produces a clean ZIP with accurate usage instructions and no Git metadata,
    runtime database, model logs, or credentials.

Interactive browser inspection remains evaluation evidence until an
owner-approved browser command emits a structured witness. It does not become
model browser authority.

## Implementation slices

### Slice 0: freeze evidence and build the paired scorer

Before changing execution:

- preserve the unrestricted and sliced Crisis Atlas artifacts;
- implement the two scorecards;
- add a paired-run manifest;
- make context/output stop reasons explicit in telemetry; and
- encode the current Crisis Atlas defects as evaluation facts.

Exit: the old arms can be rescored without model calls and Slice 2 is labeled
both a proposal miss and a false-completion/detection miss.

### Slice 1: decide the new authority boundary

Write the superseding ADR for ephemeral workcell capability versus durable
authority. Threat-model mounts, Git, processes, network, credentials, audit,
promotion, and cleanup.

Exit: the ADR explicitly permits a shell only inside the disposable workcell
and explicitly denies model authority over host state, verification,
transitions, checkpoints, and delivery.

### Slice 2: baseline CLI conformance spike

Run the actual default Qwen CLI, or a byte-for-byte tool-schema equivalent,
inside the hardened workcell. Do not add acceptance repair yet.

Compare a small corpus with the prior unrestricted control. Record tool calls,
model prompts, context behavior, filesystem effects, cleanup, and capability
differences.

Exit: no useful control capability is missing; containment tests demonstrate
that host paths, network, credentials, and controller sockets are unreachable.

### Slice 3: candidate delta admission

Freeze a checkpoint, calculate the whole delta, apply policy outside the
workcell, and reconstruct it in a clean verifier environment.

Exit: valid multi-file work is preserved without a JSON change-set envelope;
one forbidden path rejects promotion atomically; the original source tree is
unchanged.

### Slice 4: slice readiness and structured witnesses

Implement `SliceAcceptanceContract`, the new-component rule, versioned witness
schemas, and readiness-based completion.

Exit: the exact Crisis Atlas Slice 2 first proposal cannot complete even when
all inherited tests pass, and a real server/routes witness can prove the slice.

### Slice 5: context, compaction, and adaptive budgets

Implement the stable task kernel, persistent state capsule, bounded tool
output with artifact spill, proactive two-tier compaction, stable prompt-cache
prefix, and explicit ceiling stop reasons.

Replace tiny fixed turn counts with owner ceilings primarily expressed as wall
time, process time, token budget, no-progress detection, and destructive-action
policy. Retain a high emergency call ceiling.

Exit: a 64K rollover continues coherently; an output-cap hit is diagnosed; old
raw logs are not replayed; the model can still retrieve any repository artifact
on demand; and prompt-evaluation/cache telemetry proves whether the stable
prefix helped.

### Slice 5A: diagnostics and runtime performance profile

Add safe LSP/syntax feedback, the adaptive verification pyramid, read-only tool
parallelism, reasoning-effort experiments, and a hardware-specific
`llama-server` benchmark.

Exit: the chosen profile improves latency or token use without lowering any
paired proposal-quality or acceptance result; rejected optimizations remain
recorded as negative results rather than silently entering the default.

### Slice 6: authoritative repair checkpoints

Integrate local repair, genuinely stronger frontier repair, and human repair
through one `PlanCheckpoint` model.

Exit: later slices and final delivery inherit the repaired checkpoint without
out-of-band commits or stale outcome projection.

### Slice 7: negative controls and paired qualification

Run the corpus and Crisis Atlas must-pass regression. Publish per-call
telemetry, both scorecards, browser evidence, negative-control results, and
resource use.

Exit: all release gates pass. Otherwise the mode remains experimental and the
failed cases become the next bounded engineering targets.

### Slice 8: rollout and fallback

Only after qualification:

- make Capability Sandbox the recommended local mode;
- retain legacy typed Local Power behind an explicit compatibility toggle;
- expose the paired parity guard for high-assurance runs;
- document resource implications; and
- keep a one-action rollback to the previous mode.

## Required deterministic coverage

Each implementation slice must add fake-provider coverage for model-driven
branches and direct non-model coverage for controller boundaries.

At minimum cover:

- native tool-call transcript replay;
- shell/process timeouts and cleanup;
- mount, path, network, credential, and socket isolation;
- candidate freeze and fingerprint mismatch;
- atomic rejection and clean reconstruction;
- inherited tests passing with an unexercised new component;
- malformed, stale, or command-name-only witnesses;
- checkpoint request with open obligations;
- output truncation, tool-output truncation, compaction, and rollover;
- same-model frontier misconfiguration;
- local, frontier, and human checkpoint ancestry;
- negative-control detection;
- paired scorer tie, regression, and superiority cases; and
- fail-closed delivery when evidence is missing.

Run focused tests, the full deterministic suite,
`python -m compileall -q src tests`, and the repository's documented
CRLF-aware `git diff --check`. Update `HANDOFF.md`, `README.md`, and
`NEXT_STEPS.md` in every behavior-changing slice, and add a dated evaluation
record for every live run.

## Explicit non-goals

This handoff does not authorize:

- a Qwen shell on the Windows host;
- network or credential access by prompt instruction alone;
- model-selected verification or acceptance policy;
- model-owned task completion, Git promotion, plan approval, or delivery;
- self-authored tests as the only acceptance evidence;
- calling the same local Qwen endpoint “frontier”;
- hiding a regression inside an aggregate score; or
- changing the default mode before paired non-inferiority passes.

## Why this should make Apoapsis Qwen better

The current harness tries to improve reliability partly by narrowing Qwen's
action language. The control showed that the narrowing removed useful
engineering behavior: Qwen was better when it could inspect, edit, test, and
repair naturally.

The superseding design composes the strengths instead:

1. use the same capable inner agent loop that produced the stronger control;
2. contain it with operating-system boundaries rather than cognitive
   restrictions;
3. preserve context with compaction and on-demand retrieval instead of replay;
4. prevent early green tests from terminating incomplete work;
5. judge the result with plan-mapped, structured, independent witnesses;
6. send precise failures back for repair;
7. use a genuinely stronger reviewer when escalation is authorized; and
8. keep every accepted repair and delivery inside the authoritative state
   machine.

That gives Apoapsis a credible path to weakly dominate default Qwen: the
proposal engine keeps its baseline capabilities, while the outer system catches
false success and supplies evidence-driven repair that the default agent does
not have.

This handoff is the implementation contract. It is not evidence that the new
mode exists or that superiority has been achieved.

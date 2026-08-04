# ADR 0112: A run that says what it is doing

## Status

Accepted and implemented on 2026-08-03.

## Context

During a slice run the operator saw a spinner. Everything the harness knew was
true and unreachable: slice state, current turn, checkpoint verdicts and budget
burn all existed, scattered across JSON files under the task's audit directory,
and most of them did not exist *yet* at the moment the question was asked.

That is the shape of the problem. `checkpoint.json` is written after a
checkpoint. `model-usage-series.json` is written when the arm finishes.
`result.json` is written at the end. Every artifact this system produces is a
*result*, and none of them can answer "what is it doing right now", because the
answer is needed while the work that will produce them is still going.

The two status endpoints that existed were
`store.get(...).model_dump(mode="json")` — the durable operation row, which
carries `status: "running"` and nothing else. A UI polling it learned that the
run was still running, which the operator could already see.

Meanwhile ADR 0101 plumbed relay-observed usage into reports, but only as
totals computed after the fact. The single most useful live number in the
system — how big the last prompt was against the window — was collected
per-call inside the relay and then summed away before anyone could see it.

## Decision

**One artifact is written *during* the work: an append-only progress journal
at `evidence/progress.jsonl`, next to the run's other evidence.**

It records run start (carrying the context window the agent actually runs
against), stage entry and exit with the writer's own monotonic duration,
per-call model usage as the relay observes each exchange, checkpoint verdicts
including ADR 0105's operator rendering verbatim, and run finish.

A journal rather than a state file, for three reasons. A reader arriving late
reconstructs the whole run. A reader arriving mid-run sees everything so far.
A run that dies leaves its own last known position rather than a stale
"running" flag nobody cleared — the projection then reports the stage it was
in, which is exactly what the evidence says.

**Two properties are load-bearing and both are tested.**

*Reads happen mid-write.* A polled endpoint will routinely catch a
half-flushed line. `read_progress` discards a torn trailing line rather than
raising: a torn last line is the normal case, not corruption. Events are also
ordered by a writer-assigned sequence rather than by timestamp, so two events
in the same millisecond, or a host clock that steps backwards, cannot reorder
a run's history.

*Writing must never break the run.* Every append is best-effort. A journal
whose directory cannot be created, or whose disk fills, disables itself and
the slice continues. The relay's usage observer is called outside the lock and
swallows everything it raises, then detaches — an observability hook on the
path every model exchange takes must not be able to serialise the relay or
fail a slice that was otherwise going to succeed.

**The status itself is a pure projection.** `reporting/run_status.py` takes
events and returns a `RunStatus`; it opens no sockets and asks the running
process nothing. That is what makes MH-9's "deterministic tests for the
projection from recorded artifacts" achievable at all: a fixture journal
produces a status without a container, a model, or a clock to mock.

Three deliberate refusals in the projection:

- **It never invents a window.** No recorded window and no supplied one means
  no utilization percentage, not a percentage against a guessed denominator.
  When the run recorded one, that wins over anything the caller supplies — the
  caller is reading today's configuration, the run knows what it actually ran
  against.
- **It never decides a run died.** An unclosed stage with no finish event is
  reported as still running. Concluding "probably crashed" would be the
  projection inventing a fact the run never recorded.
- **It distinguishes "not yet" from "did not happen".** A stage not reached is
  `PENDING` while the run is live and `SKIPPED` once it is over, and a control
  arm the parity policy declined to schedule is `SKIPPED` with that reason
  attached from the first poll. "No control arm ran" is not evidence of
  anything; "and here is why" is (ADR 0108).

**The UI gets its own endpoint, `/api/tasks/<id>/run-status`.** Not a field on
`task_detail`: that recompiles current evidence, reads the report and walks
the event log on every call, and polling it every two seconds would make
watching a run measurably slow the run down.

**Both stage labels and the checkpoint rendering are in operator language.**
The page says "Writing code", not `MODEL_RUNNING`. The last checkpoint shows
ADR 0105's three parts — what was attempted, what refused it, the one next
action — with the harness's own words behind a disclosure. Nothing is hidden;
the ordering is the whole change.

## Consequences

- An operator watching one page can answer MH-9's three questions: what it is
  doing (the stage list, in full, including stages not yet reached, so they can
  see how long they are in for), how far along it is (elapsed per stage), and
  whether context is near the window (last prompt against the recorded window,
  with an explicit warning past 80%).
- The peak and the latest prompt size are both shown, because they answer
  different questions. A slice that compacted mid-run has a high peak and a low
  latest, and reporting either alone would hide the compaction.
- The relay gained an optional `usage_observer`. Its default is `None` and its
  failure mode is to detach, so no existing path changes behaviour.
- `LiveWorkcellSession` and `execute_slot` gained a pass-through parameter for
  the same observer. Both default to `None`.
- The journal is one more artifact per attempt. It is small — a few hundred
  bytes per model call — and it is the only artifact that survives a killed
  run with useful content in it.
- The controller image prebuild half of MH-9 is **not** in this change. It
  touches the launcher and operator lifecycle rather than the status surface,
  and it is recorded in NEXT_STEPS as its own item. The build stage exists in
  the pipeline vocabulary (`RunStage.CONTROLLER_BUILD`) and will populate as
  soon as something emits it.

## Alternatives rejected

**Infer the stage from which files exist and their mtimes.** No new artifact,
and it was tempting. But it cannot answer the context question at all until
the arm finishes, and mtime-derived stage timing is a guess dressed as a
measurement. The status view's whole value is that its numbers are the run's
own.

**Have the worker write a status row to the operations database.** A single
mutable row loses history, cannot express "the run died here", and puts a
write on the execution path that has to succeed. The journal's failure mode is
"no status page"; a database write's failure mode is a failed operation.

**Push updates over a websocket instead of polling.** More responsive, and a
long-lived connection into a process that is deliberately allowed to die
mid-run is a much worse thing to reason about than a poll that returns
whatever is on disk. Polling also means the page is correct after a refresh,
a restart, or a week later.

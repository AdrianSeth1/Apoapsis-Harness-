# ADR 0113: Warm the controller image before a slice waits on it

## Status

Accepted and implemented on 2026-08-03. Verified against real Docker.

## Context

`tools/run_capability_sandbox_task.sh` builds the controller image inline:

```
if ! docker image inspect "${TAG}" >/dev/null 2>&1; then
  bash "${REPO}/docker/pilot-controller/build.sh" "${COMMIT}" "${TAG}" "${REPO}"
fi
```

`TAG` is `apoapsis-product-controller:${COMMIT:0:12}` — the *harness* commit —
and `build.sh` passes `--no-cache` for a good reason it documents: a cached
`LABEL` layer retains the build args of whichever build first created it, so a
cached rebuild can carry a build-context digest belonging to a different
context.

Both decisions are right. Together they have a consequence nobody chose:
**every commit to Apoapsis makes the next plan slice pay a full image build,
inside its own critical path, before any model work, with nothing on screen.**

Measured on the owner's machine on 2026-08-03: twenty-four
`apoapsis-product-controller` tags at 424 MB each — roughly ten gigabytes, one
per commit — and the tag for the then-current `HEAD` **absent**, meaning the
next slice would have paid a build. A real build of that image took **34
seconds**.

Thirty-four seconds is not the catastrophe the original review implied. It is
also not nothing: it is half a minute of unmoving spinner at exactly the
moment an operator is waiting to learn whether their slice works, and it
recurs every time the harness is committed to — which, during active
development, is most runs.

## Decision

**The launcher warms the image; the slice does not build one it could have
had already.** `operator_lifecycle start` — what `START_APOAPSIS.cmd` already
calls — now builds the controller image for the current harness commit if it
is missing. `start_local_models` already owns "make the slow things happen
before the operator is waiting on them"; a Docker build belongs there next to
a model load.

**Warming is never fatal.** Every failure path returns a status carrying a
reason rather than raising: no Docker daemon, a build that fails, a build that
exceeds its timeout, or any unexpected exception. Intake, planning, review and
the entire UI work with no Docker at all, and a launcher that refused to start
because a sandbox image could not be warmed would trade a large capability for
a small one. The "Docker is not running" message says so explicitly, because
otherwise a warning about a sandbox image reads as a broken install.

**The build reports itself as a stage.** The build happens on the host before
the controller container exists, so the controller cannot record it — which is
why `RunStage.CONTROLLER_BUILD` existed in ADR 0112's vocabulary with nothing
emitting it. The launch script now appends the stage to the same
`evidence/progress.jsonl` the controller appends to, on **both** paths: it
records "image was already built" as well as a real build, because "the image
was already there" is the answer to "why was this run fast", and a stage that
silently never appears cannot answer it.

That makes the journal a two-writer file, so `ProgressJournal` now resumes the
existing sequence instead of restarting at 1. Sequence is what the projection
orders on — precisely so it never has to trust a clock shared across a
container boundary — and two events numbered 1 would be ordered arbitrarily.

**Stale images are reported, never deleted.** `stale_controller_images` lists
per-commit tags that are not current, keeping the two most recent besides it
(the usual reason to want an older image is comparing a result against the
harness that produced it, and that is nearly always recent).
`prune_controller_images` takes an explicit list, is never called
automatically, and refuses any tag outside its own repository even when asked
— a caller that computed its list wrongly must not be able to delete an
unrelated image through it. Reclaiming disk is an operator's decision, not a
side effect of starting an app.

**One tag rule, two implementations, one drift guard.** The shell computes the
tag before deciding whether to build; Python computes it before deciding
whether to warm. A disagreement would not fail — it would silently build a
second copy of an image that already exists under another name, doubling both
the wait and the disk. A test asserts the repository name and the commit
length in `controller_image.py` still match the strings in the shell script.

## Consequences

- After a harness update the wait moves from "the operator clicked Run and
  nothing happened for 34 s" to "the app took a moment to open" — a wait
  people already understand, and one that is not blocking a decision.
- When a build does happen inside a slice — a `--no-prebuild-sandbox-image`
  launch, or a harness updated while the app was open — it is now a named,
  timed stage on the status page instead of an unexplained gap.
- `apoapsis-model-lifecycle start` gained `--no-prebuild-sandbox-image` for
  operators who do not use the sandbox and do not want a Docker call at start.
- The launcher's JSON result gained `capability_sandbox_image`, so what
  happened is recorded rather than inferred from timing.
- Nothing prunes automatically. The ten gigabytes observed on the owner's
  machine are still there, and now visible.

## Verification

Not theory: exercised against the real toolchain on 2026-08-03.

- `prebuild_controller_image` on a machine whose current-commit image was
  absent built `apoapsis-product-controller:e0113c59d24e` through WSL2 and
  Docker Desktop in **34.3 s**, and the image carries
  `org.apoapsis.source-commit=e0113c59d24e…` in its labels.
- Called again immediately, it returned `already_present` in **0.4 s** without
  invoking Docker's builder — the idempotence the launcher depends on.
- The `progress_event` shell function was extracted from the launch script and
  run under a real bash; its output parses through `read_progress`, projects
  to a `DONE` "Building the sandbox image" stage carrying its duration, and a
  `ProgressJournal` opened afterwards continued the sequence at 3 rather than
  colliding at 1.
- That last check is a test (`ShellWritesAJournalThePythonSideCanReadTests`),
  skipped where no working bash exists. On Windows `shutil.which("bash")`
  finds the System32 WSL shim, which resolves to the default distribution —
  `docker-desktop` here, which has no `/bin/bash` — so the test probes by
  *running* bash rather than trusting the path, and says so when it skips.

## Alternatives rejected

**Drop `--no-cache` so builds are fast enough not to matter.** It would make
the image's provenance labels untrustworthy, which is the thing the whole
qualification argument rests on. The build script already explains why; this
is not the ADR to overturn it.

**Tag the image by the Dockerfile and source-tree digest instead of the
commit.** Genuinely better — most commits do not change `src/`, so most
rebuilds are avoidable entirely rather than merely moved earlier. It is also a
change to how a running slice identifies the code that judged it, which
deserves its own decision and its own evidence rather than riding along with a
UX fix. Recorded in NEXT_STEPS.

**Prune old images automatically during warm.** Ten gigabytes is a real cost
and the temptation was real. But an image the harness considers stale may be
exactly the one someone is mid-way through comparing against, and deleting an
artifact that could be evidence is not something a launcher should do while
the operator is looking at a splash screen.

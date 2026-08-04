# Demo recording script — three minutes

A shot list for a screen recording, written so the same three minutes can be
re-recorded after any change without re-deciding what to show.

**Recording itself is the owner's job.** This file exists so it is repeatable
and so the run is prepared rather than improvised — an unprepared take is where
the four-minute image build and the "wait, which folder was that" pause come
from.

**Target:** an engineer who has never seen this project, watching without
sound, deciding in the first fifteen seconds whether to keep watching.

---

## Before you hit record

Do all of this first. None of it is interesting to watch.

- [ ] `git status` is clean, on the commit you want to show.
- [ ] Full suite is green — the demo asserts it, so it must be true today.
- [ ] Docker Desktop running; WSL2 `Ubuntu-24.04` up.
- [ ] **Warm the controller image**, or the first slice pays ~34 s of nothing:
      `py -3 -m apoapsis.operator_lifecycle start --project-root <demo project>`
      — it warms as part of start. Confirm with
      `docker images apoapsis-product-controller`.
- [ ] No `llama-server` already resident (the launcher refuses; a second copy
      of the weights will not fit).
- [ ] Demo project prepared at a **short path with no spaces**, plan already
      imported and approved, one slice packaged and *not* started.
- [ ] Terminal font at 16pt+. A 10pt terminal is unreadable when the video is
      scaled into a chat window.
- [ ] Browser at 1280×800, zoom 100%, bookmarks bar hidden.
- [ ] Close Slack, mail, notifications.

Rehearse once end to end. Time it. If it runs over three minutes, cut Shot 5,
not the verdict.

---

## Shot 1 — What this is (0:00–0:20)

**Screen:** `README.public.md` open, scrolled to the architecture diagram.

**Do:** nothing. Hold still on the diagram for a beat, then scroll slowly to
the headline-result table and stop.

**Caption:**
> A control system for untrusted local coding models. The model proposes.
> The harness owns verification and completion.

Do not narrate the architecture. The diagram either lands in five seconds or it
does not, and talking over it does not help.

---

## Shot 2 — It runs with no GPU (0:20–0:45)

**Screen:** terminal, in the repo.

**Type:**

```bash
python -m unittest tests.test_vertical_slice \
                   tests.test_capability_sandbox_product \
                   tests.test_workcell_checkpoint
```

**Show:** `Ran 51 tests ... OK` — about six seconds.

**Caption:**
> Every model-driven branch has deterministic fake-provider coverage.
> No GPU, no network, no Docker.

This shot exists because the reviewer's real question is "can I run any of this
myself?" and the answer needs to arrive before minute one.

---

## Shot 3 — Start a real slice (0:45–1:15)

**Screen:** the Apoapsis UI, task page, Control Room tab.

**Do:** click **Start execution**. Let the confirmation panel render and hold
for two seconds — it states the sandbox mode, the network denial, the
continuation limit and the parity policy *before* anything runs.

**Caption:**
> Everything it is about to do, stated before it does it.

---

## Shot 4 — Watch it work (1:15–2:15)

**Screen:** the same page, live status panel.

**Do:** nothing. Let it run. This is the shot.

**Point the cursor at, in order:**
1. the stage list — building the image / checking the sandbox is sealed /
   loading the model / writing code
2. the model-call count ticking up
3. **context now, as a percentage of the window**

**Caption:**
> Live, from the run's own record. Not a spinner.

If the run is slower than the recording, cut to a later point — do not speed up
the video. A 4× timelapse of a status page reads as a hidden failure.

---

## Shot 5 — The verdict (2:15–2:45)

**Screen:** the checkpoint result.

**Do:** show the three-part operator rendering — what was attempted, what
refused it (or that nothing did), and the one next action. If the slice is
`CONTINUE`, **show that instead of re-running to get a green one.** A refusal
with a readable reason is a better demo than a pass; it is the whole product.

**Then** expand the harness's own wording underneath.

**Caption:**
> Completion is decided by re-running the project's own tests under a coverage
> trace — and checking the new code actually ran.

---

## Shot 6 — The receipts (2:45–3:00)

**Screen:** split — `docs/crisis-atlas-experiment.md` headline table on one
side, `docs/evaluation/` file listing on the other.

**Caption:**
> I measured this against an unrestricted baseline. The baseline beat my first
> protocol. I rebuilt the architecture around that result.

End on the experiment writeup, not on the UI.

---

## Things not to do

- **Do not cut around a failure.** If the slice fails on camera, that is the
  take — the harness catching its own arm is the point. Re-recording until it
  passes produces a demo of something that is not this system.
- **Do not show the terminal firehose.** Container logs read as noise.
- **Do not explain the internal vocabulary — do not use it.** The harness's
  operation ids, container names and gate terminology mean nothing to a viewer
  and cost you seconds you do not have. The captions above deliberately use
  none of them; if a caption you add needs a term defined, cut the caption.
- **Do not add music.**
- **Do not record before warming the image.** Thirty-four seconds of nothing
  is a third of your budget.

## After

- [ ] Watch it once with sound off — captions must carry it alone.
- [ ] Check no absolute path, project name, token or credential is legible in
      any frame.
- [ ] Under three minutes.

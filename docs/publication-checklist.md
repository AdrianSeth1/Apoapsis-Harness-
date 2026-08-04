# Publication checklist

Everything to settle before this repository is public. Ordered so the
irreversible decisions come first: history and licence are hard to walk back,
a README is not.

---

## 1. Decide the licence *first*

`LICENSE.txt` is currently **PolyForm Noncommercial 1.0.0** — free for
noncommercial use, commercial use not granted.

That is a real decision to make consciously, because it interacts with the
reason for publishing:

- It is **not** an OSI-approved open-source licence, and GitHub will label it
  as non-standard.
- A reviewer who wants to try an idea from this at work technically cannot.
- If the purpose is to be read and to demonstrate judgement, that may be
  exactly right — it keeps the work yours.
- If the purpose is adoption, it is the wrong licence.

- [ ] Confirm PolyForm Noncommercial is the intent, or change it before the
      first public commit.
- [ ] Check no vendored file carries a conflicting licence.
- [ ] Confirm the pinned upstream components named in the manifest (Qwen Code
      CLI, llama.cpp, base images) are only *referenced*, never redistributed.

---

## 2. Decide the history

The working history contains real project paths, experiment scratch, and
commits from a period when evidence files were larger and looser.

- [ ] **Recommended: publish with fresh history.** Squash to a single initial
      commit on a clean branch, or export the tree into a new repository. The
      ADR trail already preserves the decision history in readable form, which
      is what a reviewer actually wants — nobody reads 400 commit messages, and
      the ADRs are better written than the commits.
- [ ] If you keep the history, scan every commit — not just the tree — for
      secrets and personal paths. `git log -p | grep -i` for key names is the
      minimum. Deleted-in-HEAD is not deleted in history.
- [ ] Drop the `substrate-v0.1` tag or confirm it means something to a
      stranger.

---

## 3. Exclude

`.gitignore` already covers the big ones. **Verify rather than trust it** — the
question is not what the ignore file says, it is what is actually *tracked*,
and adding a path to `.gitignore` does not untrack what is already in.

Measured on 2026-08-03: **3,921 tracked files.**

### 3a. The 2.4 GB problem — fix this before anything else

```
tracked under spikes/native-shell-tauri/src-tauri/target/ : 3,101 files
                                                            2,424.9 MB
everything else in the repository                         :   807 files
```

`.gitignore` *does* list that target directory. It was committed before the
rule existed, so the rule does nothing: **79% of the tracked files and roughly
2.4 GB of the repository are Rust build output from an abandoned desktop-shell
spike** (ADR 0050, explicitly recorded as a disposable, unbuilt spike).

A reviewer cloning that waits several minutes to download build artifacts.
GitHub will warn about the size. It is also the single easiest thing to fix.

- [ ] `git rm -r --cached spikes/native-shell-tauri/src-tauri/target` and
      commit — or, better, drop the whole spike from the published tree and
      let ADR 0050 describe it.
- [ ] If you publish with fresh history (§2) this disappears for free, *as long
      as the tree you export excludes it*. Verify after exporting, not before.
- [ ] Confirm the published clone is tens of megabytes, not gigabytes.

### 3b. Everything else

Already ignored and confirmed clean: `.apoapsis/` (0 tracked),
`.apoapsis-eval/` (0 tracked), `.sol/`, `.venv/`, `__pycache__/`.

- [ ] **`FPKG-A11D933EADC7-response.json` in the repo root is tracked.** It is
      a real planning-handoff response. Decide deliberately whether it ships;
      it does not belong in the root either way.
- [ ] Two run artifacts under `docs/evaluation/` are tracked
      (`slice-7p3-rehearsal-v8-evidence/rehearsal-report.json`,
      `slice-7p4-live-smoke-evidence/smoke-result.json`). These are *intended*
      evidence — keep them, but read them once for absolute paths.
- [ ] **Six documents cite `/home/arya/...` paths.** Sanitise or accept; a
      home directory name is not a secret but it is noise a stranger reads as
      carelessness.
- [ ] No model weights or GGUFs tracked.
- [ ] `docs/evaluation/` — keep it. It is the most persuasive material here.
      Read each file once for host paths and project names first.

---

## 4. Scan for secrets

- [ ] `git ls-files -z | xargs -0 grep -nEi "api[_-]?key|secret|token|bearer|password"`
      and read every hit. Most will be *names of environment variables*, which
      are fine; you are looking for values.
- [ ] Confirm no `.env`, `*.pem`, `*.key`, `id_rsa` is tracked.
- [ ] Check the pinned manifests and lock files for anything host-specific.
- [ ] Run a secret scanner (`gitleaks detect`) over the final tree **and**, if
      you keep history, over the full log.

---

## 5. The suite must be green on a clean clone

This is the one a reviewer actually executes, and a red suite ends the review
(see [ADR 0111](adr/0111-a-green-suite-for-strangers.md)).

- [ ] Clone the *published* repo into a fresh directory — not the working copy,
      which has untracked files that may be load-bearing.
- [ ] `python -m unittest discover -s tests` exits 0.
- [ ] Record the numbers in the README you publish: currently **2,141 tests,
      49 skips, ~19 min** on Python 3.14.5 / Windows 11.
- [ ] Run it once on **Linux or WSL2** as well. Eight tests skip on Windows
      because the host cannot carry a relay socket a Linux container can
      connect to, and those are the only deterministic coverage of the relay
      fault machinery. A green Windows run plus a green Linux run is the pair
      the claim needs.
- [ ] Confirm every skip reason names its missing capability.

---

## 6. Make the first ten minutes work

- [ ] `README.public.md` becomes `README.md` in the published repo, and the
      current operator guide moves to `docs/operating-guide.md`. Do not publish
      both as `README*.md` — a reviewer should never have to pick.
- [ ] Every link in the public README resolves in the published tree.
- [ ] The Mermaid diagram renders on GitHub (view the file on the web, not just
      locally).
- [ ] The quickstart commands work **on a clean clone, copy-pasted, on a
      machine without a GPU.** Actually do this. It is the single most common
      thing to be broken and the most damaging.
- [ ] The demo recording is linked near the top.

---

## 7. Last read-through

- [ ] Read `README.public.md` and `docs/crisis-atlas-experiment.md` end to end
      as a stranger. Any sentence needing internal vocabulary is a bug.
- [ ] Every number in both documents traces to a file in `docs/evaluation/`.
- [ ] The limitations sections are still true and still honest. They are the
      most credible part of the package; do not let them drift into marketing.
- [ ] Nothing claims a live result that was only measured deterministically.

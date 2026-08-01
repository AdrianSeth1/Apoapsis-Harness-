# ADR 0093: Friendly launcher-owned project setup

- Status: Accepted
- Date: 2026-08-01

## Context

The primary Windows launcher required operators to create a Git repository and
run `apoapsis init` before the browser could open. That exposed implementation
details as two separate terminal chores and made the normal first-run path fail
for people who do not use command-line tools.

Automatic setup must not turn folder selection into authority to capture or
commit arbitrary user files. It must also preserve the existing rule that
Apoapsis runtime state does not make the project worktree dirty.

## Decision

The trusted Windows launcher runs a deterministic controller-side preparation
step before starting the model service or UI:

- An empty folder becomes a Git repository on `main`, receives Apoapsis runtime
  state and repository-local ignore entries, and gets one empty initial commit.
- An existing Git project with a commit receives missing Apoapsis runtime state
  and repository-local ignore entries without modifying tracked files.
- A non-empty non-Git folder, a repository with user files but no first commit,
  or a selected subfolder inside another Git project is refused with plain
  guidance before Apoapsis runtime state is written.

The initial commit uses an ephemeral command-scoped identity and contains no
user files. The launcher never stages or commits pre-existing content. It uses
`.git/info/exclude`, including Git's resolved worktree-aware location, instead
of editing the project's `.gitignore`. The explicit `apoapsis init` command
retains its existing `.gitignore` behavior for terminal users.

This preparation is controller code. No model receives Git, filesystem,
workflow, or initialization authority.

## Consequences

A nontechnical operator can select or create an empty folder and proceed in one
step. Existing projects can also be opened without a separate initialization
command or an immediate tracked worktree change.

Folders whose contents would require a human Git decision still stop safely.
The launcher does not install software, download models, clone repositories, or
infer whether existing files are safe to commit.

## Verification

Deterministic coverage exercises empty-folder setup, an empty unborn Git
repository, an existing committed project, idempotent reopening, refusal of
non-Git folders containing files, refusal of unborn repositories containing
files, and refusal of nested subfolders. Launcher source checks bind setup
before lifecycle and UI startup. The focused setup/launcher run passed 23/23;
a broader setup/launcher/CLI/Architect-UI regression run passed 46/46.
The full Windows run reached 1,961 tests with 36 skips, 6 failures and 11
errors. One failure was a stale exact-copy assertion introduced by the UI text
change; it was corrected and its 16-test module rerun green. The remaining
failures/errors are the existing Windows filesystem and Docker qualification
inventory, and the full 19-minute run was not repeated. `compileall`,
JavaScript syntax validation, and diff check also passed. No model or provider
was started for this change.

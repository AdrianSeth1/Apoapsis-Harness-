# ADR 0017: Shared worktree fingerprint, and explicit acceptance designation

- Status: Accepted
- Date: 2026-07-18

## Context

`BoundedAgentSession._verification_state_digest()` (introduced with ADR
0015/0016's acceptance coverage) scoped verification caching, per-command
execution results, and acceptance proof to `hashlib.sha256(git diff
--no-ext-diff HEAD)`. That digest is blind to **untracked files** --
anything a patch created without `git add`ing it, which is the normal
outcome of applying a unified diff through `patches/apply.py` (`git apply`
without `--index`). A model could therefore create or edit a new file and
the verification-state digest would not change, meaning an
already-recorded pass could, in principle, keep "proving" a criterion after
the code backing it had changed. This is a proof-integrity gap, not a
theoretical one: it is the direct, common byproduct of ordinary
`propose_patch`/`replace_text` use.

Separately, `apoapsis init`'s config (ADR 0016) marked its one generated
command `acceptance = true` automatically, so a fresh `STRICT` project was
"immediately usable" -- but that also meant the harness, not the owner, was
deciding that a generic unit-test pass constitutes product-level proof.
That decision belongs to the owner alone.

## 1. One shared, deterministic worktree fingerprint

`src/apoapsis/repository/fingerprint.py` adds `compute_worktree_fingerprint
(worktree) -> WorktreeFingerprint`, replacing the old digest everywhere
verification caching, command results, and acceptance proof are scoped
(`BoundedAgentSession._verification_state_digest`, the sole caller of the
old digest -- one-shot mode never needed digest-scoping, since it checks
coverage immediately after a single verification run with no caching
across turns). The fingerprint combines:

- **HEAD identity** (`git rev-parse HEAD`).
- **Canonical tracked diff bytes**: `git diff --no-ext-diff --unified=0
  HEAD`, hashed. Zero context lines keep the fingerprint sensitive only to
  what actually changed, not to how much surrounding text git happens to
  print; evidence-facing diffs elsewhere (`RepositoryInspector.diff()`,
  repair-prompt diffs) intentionally keep larger context for readability
  and are unaffected by this choice.
- **Sorted, non-ignored untracked paths**: `git ls-files --others
  --exclude-standard`, filtered through the same forbidden-top-level-
  directory policy (`.git`/`.apoapsis`/`.sol`) already used by
  `RepositoryInspector`/`patches/validator.py`, so the harness's own
  bookkeeping can never enter a fingerprint even on a target project whose
  own `.gitignore` doesn't happen to exclude it.
- **Exact content hashes and type/mode** for every permitted untracked
  path: a regular file's raw bytes are hashed directly (works identically
  for text or binary, no decoding); a symlink's literal target text is
  hashed via `os.readlink()` -- **never dereferenced, never followed** --
  recorded with kind `symlink` and mode `120000`, matching Git's own mode
  convention. Directories/sockets/other non-file entries are not
  fingerprinted (nothing to hash).

The whole structure is serialized to canonical JSON (`sort_keys=True`) and
hashed once more into a single `digest` string -- the same shape of
composition already used for content digests elsewhere in the codebase
(`ContextPackage.context_sha256`, `EscalationPackage.current_diff_sha256`).

**Deterministic handling, not rejection, for untracked symlinks/binaries in
the fingerprint itself**: an untracked symlink or binary file's mere
presence must never be a blind spot, so both are hashed safely (link text,
raw bytes) rather than causing the whole fingerprint computation to fail
or silently skip that entry. Rejection is instead applied at the
**exposure** layer (below), consistent with existing policy
(`patches/validator.py` unconditionally rejects symlink/binary *changes*;
`RepositoryInspector.read()` unconditionally rejects binary *content*).

## 2. Every caching/proof site uses the shared fingerprint

`BoundedAgentSession.command_results: dict[str, dict[str, VerificationStatus]]`
(renamed from the ADR 0016 `passed_checks`) is keyed by
`fingerprint.digest` exactly as before, and `_verify()`/
`_all_required_checks_passed()`/`_check_completion()` needed no further
changes beyond swapping the digest source -- the tri-state, digest-scoped
guarantee from ADR 0016 (never-executed / failed / passed, never stale)
now correctly spans tracked *and* untracked file changes.
`tests/test_acceptance_coverage.py` adds an untracked-file variant of the
existing tracked-file staleness tests: a mapped acceptance command passes,
`propose_patch` creates a brand-new file (staying untracked, per normal
`git apply` behavior), and the criterion correctly reverts to Unproven
until the same command is re-run against the new fingerprint -- proving
exactly the gap this ADR closes. `tests/test_worktree_fingerprint.py`
covers the fingerprint function directly: tracked edits, untracked
creation, untracked editing, determinism across repeated calls, ignored
harness directories, and untracked binary/symlink hashing.

## 3. `inspect_diff` shows the same state the fingerprint is sensitive to

`RepositoryInspector.diff()` now appends a bounded, synthetic "new file"
unified diff for every permitted untracked path (`--- /dev/null` / `+++
b/<path>` / `@@ -0,0 +1,N @@`), built with the same
`list_permitted_untracked_paths()` helper the fingerprint uses, so a model
can literally see the untracked-file state the verifier is now checking
against. **Exposure fails closed for binary and symlink content**,
consistent with the codebase's existing refusal to treat either as safe
text: a binary untracked file is represented as a single `Binary files
/dev/null and b/<path> differ` line (path visible, bytes never rendered); a
symlink is represented as a `Symlink /dev/null and b/<path> differ (symlink
target withheld)` line (path visible, real target text never rendered).
Both keep the model aware such a file exists -- for genuine text files it
sees full added content -- without ever decoding/dumping bytes or a
symlink's real target into model-visible context. The whole combined diff
remains subject to the same `max_chars` bound already applied to tracked
diffs.

## 4. Acceptance designation is never generated automatically

`apoapsis init`'s template keeps `completion_policy = "strict"` (ADR
0016's practical default is unchanged), but its one generated command now
writes `acceptance = false` explicitly, with an inline comment explaining
that acceptance designation is an owner decision to be made once a real
mapping is decided, referencing this ADR. `CompletionPolicy`'s Pydantic
field default remains `BASELINE` for hand-built configuration, unchanged
from ADR 0016. No migration path rewrites an existing
`.apoapsis/config.toml` -- this is a template change for newly generated
projects only.

## 5. Deterministic Doctor warnings, no silent migration

`doctor.py` gains `_completion_policy_checks()`:

- `STRICT` with zero `acceptance = true` commands → `WARNING`
  (`completion_policy_acceptance_commands`): any task with active
  acceptance criteria will correctly stop at `HUMAN_REVIEW_REQUIRED`
  instead of reaching `COMPLETE`, and the owner should know that before
  running one.
- `BASELINE` selected → `WARNING` (`completion_policy_baseline`): `COMPLETE`
  is reported whenever configured verification passes, with no acceptance
  criterion required to be proven; the remediation notes that `apoapsis
  eval` deliberately keeps `BASELINE` for false-success measurement, so
  this warning is about an ordinary product run's persisted configuration,
  never about an evaluation run (Doctor only ever reads the persisted
  `.apoapsis/config.toml`, never an eval lane's in-memory overlay).

Both checks only report; neither rewrites the configuration file. The
existing generic `apoapsis ui` Doctor view (ADR 0014) already renders every
check by iterating `doctor.checks`, so no new UI code was needed for these
warnings to appear. `ui/application.py`'s `_execution()` overview also now
surfaces `completion_policy` directly (visible without running Doctor), and
the "Models & environment" page shows it alongside the existing execution
metrics.

## Authority and safety (unchanged)

Nothing here grants a model new authority. The fingerprint is computed
entirely by the harness from real filesystem/git state; a model cannot
influence what it hashes beyond the ordinary consequence of the edits it
already had authority to propose. `inspect_diff` remains a bounded,
read-only observation action. Doctor remains read-only and never rewrites
project configuration. The full pre-existing suite (210 tests before this
change) is unaffected; 17 new tests were added (227 total, 6 intentional
skips -- 2 new ones for symlink creation being unsupported on this Windows
machine, matching the existing Docker-backend symlink-skip precedent).

## Non-goals

- No migration of existing `.apoapsis/config.toml` files -- this only
  changes what `apoapsis init` writes for new projects.
- No change to one-shot mode's completion path (it never needed
  digest-scoping).
- No change to retrieval, context compilation, patch policy, or the
  held-out oracle.
- No new agent action; `inspect_diff`'s existing action just returns more
  complete evidence.
- Directory-level reparse points/Windows junctions among untracked entries
  are out of scope here (the existing Docker-backend workspace-copy
  junction defenses, ADR 0009, are unrelated and unchanged); this ADR
  covers individual untracked *files* returned by `git ls-files --others`.

## Consequences

Verification caching, per-command results, and acceptance proof now share
one fingerprint that is sensitive to every file change a normal agent turn
can produce, tracked or not, closing a real gap between what the verifier
checks and what the model can silently change. A model can now also
inspect the same untracked-file state being fingerprinted, without ever
receiving raw binary bytes or symlink targets as if they were safe text.
Acceptance designation is once again purely an explicit owner decision,
surfaced clearly by Doctor and the UI rather than defaulted quietly.

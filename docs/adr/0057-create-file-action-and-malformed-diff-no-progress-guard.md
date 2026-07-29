# ADR 0057: `create_file` action, and a malformed-unified-diff no-progress guard

- Status: Accepted
- Date: 2026-07-25

## Context

A Test Project 3 audit (`.apoapsis/tasks/TASK-F53F89DD2F852E2FBBA18A2D`) of a
from-scratch Gmail-OAuth2 slice run against a local Qwen3-Coder-Next model
showed the bounded local coder repeatedly failing to hand-author a
new-file unified diff: turns 1-8 emitted `propose_patch` diffs missing `+`
markers on continuation lines inside a new-file hunk
(`UnifiedDiffError: patch contains non-diff content inside a hunk`), and a
later attempt to create a second new file (a test) embedded a second
`diff --git` header inside a still-open hunk. Both are structural
diff-syntax mistakes, not implementation mistakes -- the model's
understanding of what the file should contain was fine; its ability to
reliably emit the exact `diff --git a/path b/path` / `new file mode
100644` / `--- /dev/null` / `+++ b/path` / `@@ -0,0 +N,M @@` /
`+`-prefixed-body syntax was not. The harness's existing repeat-failure
guard (ADR 0046) already recognizes and stops three consecutive no-op
edits and three consecutive whitespace-rejected patches, but its two
classifier substring tuples
(`_NO_PROGRESS_EDIT_REJECTION_SUBSTRINGS`,
`_WHITESPACE_PATCH_REJECTION_SUBSTRINGS` in
`src/apoapsis/agent/session.py`) did not include `UnifiedDiffError`
messages at all, so a model stuck in this failure mode burned through the
*entire* `max_patch_attempts`/`max_turns` budget one malformed diff at a
time instead of being redirected or stopped early.

`replace_text` already solves the equivalent problem for *editing* an
existing file: the model gives `old_text`/`new_text` and the harness
(`RepositoryInspector.replacement_patch`) builds the validated diff
itself, so the model never hand-authors diff syntax for an edit. No
equivalent existed for *creating* a file -- new-file creation was only
reachable through `propose_patch`, and `replace_text` explicitly refuses
any path not already in the repository.

## Decisions

1. **New `create_file` action** (`AgentActionKind.CREATE_FILE`,
   `CreateFileAction(path, content)` in `src/apoapsis/agent/actions.py`).
   `content` is plain literal file text -- no diff markers, no `diff
   --git`/`+`/`@@` syntax at all. `RepositoryInspector.new_file_patch`
   (`src/apoapsis/agent/inspection.py`) converts it into a well-formed
   `new file mode 100644` unified diff using the same `difflib.unified_diff`
   technique `replacement_patch` already uses for edits, so the diff the
   parser/applier/policy validator sees is always harness-authored and
   well-formed; the model cannot get the diff syntax wrong because it never
   writes any. `new_file_patch` refuses a path that already exists
   (directing the model to `replace_text`/`propose_patch` instead) and empty
   content. `create_file` is dispatched through the same
   `_apply_patch_action` funnel as `propose_patch`/`replace_text`
   (`BoundedAgentSession._execute` in `src/apoapsis/agent/session.py`), so it
   is subject to the same patch-attempt budget and policy validation
   (file-count/line-count/path-escape/dependency/test/verification-file
   rules) -- this is a safer *input* to patch creation, not a bypass of
   patch policy.
2. **`UnifiedDiffError` joins the repeat-failure classifiers.** A new
   `_is_malformed_diff_rejection` predicate
   (`_MALFORMED_DIFF_REJECTION_SUBSTRINGS = ("UnifiedDiffError:", "the
   previous two propose_patch attempts")`) recognizes any parser-level
   diff-syntax failure, not just the specific "non-diff content inside a
   hunk" message from the observed audit -- `"invalid diff --git header"`
   and `"does not contain a hunk"` are also `UnifiedDiffError` messages and
   are covered the same way.
3. **After exactly one malformed-diff rejection**,
   `_next_action_requirements` now injects explicit guidance recommending
   `create_file` for new files and `replace_text` for existing ones,
   mirroring the existing one-strike guidance for whitespace-rejected
   patches.
4. **After two consecutive malformed-diff rejections, a third
   `propose_patch` is refused outright** before it is even attempted
   (`BoundedAgentSession._repeated_malformed_diff_block`, checked in
   `_execute`'s `ProposePatchAction` branch). This does not consume a patch
   attempt -- it is a pre-emptive redirection, the same accounting treatment
   `replacement_patch`'s identical-old/new-text check already gets. The
   block's own rejection message matches the same classifier substring
   tuple, so a model that keeps sending `propose_patch` after being blocked
   is still recognized as the same failure class.
5. **Three consecutive same-class rejections still stop the session early**
   (a fourth pattern added to
   `BoundedAgentSession._repeated_no_progress_stop_reason`, alongside the
   three ADR-0046 patterns), returning `ESCALATION_REQUIRED` with reason
   `"coding model repeated a malformed unified diff three times without
   switching to a safer action"` rather than draining the remaining
   turn/patch budget. In practice this means: attempt 1 fails and gets
   redirected, attempt 2 fails and arms the block, attempt 3 is blocked
   (free, no budget spent) and itself counts toward the three-strike window,
   so a model that still won't switch approaches stops at that point instead
   of attempt `max_patch_attempts`.
6. **Prompt updates** (`src/apoapsis/models/prompts.py`,
   `_AGENT_STEP_STATIC_PREFIX`): `create_file` is listed in
   `ALLOWED_ACTIONS` and `ACTION_RULES` now recommends it ahead of
   hand-authoring a new-file `propose_patch` diff, including in the
   from-scratch-task ("REPOSITORY_EVIDENCE showing (none)") guidance that
   previously told the model to `propose_patch` a new-file diff directly.
7. **No change to any authority boundary.** `create_file` still goes through
   `_apply_patch_action`, the patch-attempt budget, `PatchPolicyValidator`,
   and `GitPatchApplier`/`git apply --check` exactly like every other edit
   path; the harness still owns patch application, verification, retry
   limits, escalation, and completion. The pre-emptive block is a rejection
   the harness issues from already-recorded turn history, not a new grant of
   model authority.

## Non-goals

- **Not a fix for `replace_text`'s existing no-op detection**, which
  already works (`old_text == new_text` raises before a patch attempt is
  spent) and is unrelated to this ADR's failure mode.
- **Not a change to `UnifiedDiffParser`'s new-file-hunk auto-repair**
  (`_canonicalize_new_file_hunks`, single-hunk case only). That repair path
  is untouched; `create_file` is an alternative for models that keep
  missing it, not a replacement for it, since `propose_patch` remains
  available and sometimes necessary (multi-file patches, edits combined
  with new files in one patch).
- **Not a live Laguna S 2.1 (`laguna-s-2.1:IQ4_XS`) run.** This ADR pins
  the harness-side behavior with deterministic fake-provider tests first,
  per the owner's explicit request; re-running the same fake-provider
  scripts (and, separately, a live Ollama session) against Laguna is
  follow-up work, not part of this change.
- **No change to `max_patch_attempts`/`max_turns` defaults** (see ADR
  0049); this ADR only changes when the session stops or redirects within
  the existing budget, not the budget itself.

## Tests

`tests/test_agent_loop.py`, `BoundedAgentIntegrationTests`:
`test_repeated_malformed_new_file_diffs_are_blocked_then_stopped`
reproduces the exact audited `UnifiedDiffError: patch contains non-diff
content inside a hunk` message from a missing-`+`-marker new-file diff,
scripts three consecutive `propose_patch` attempts, and asserts the
session stops at `agent_turns == 3` with only `agent_patch_attempts == 2`
spent (the third attempt was blocked, not applied), that the second
model-call prompt already contains the `create_file`/`replace_text`
redirection guidance, and that the third turn's summary matches the
pre-emptive block. `test_create_file_creates_a_new_file_without_hand_authored_diff_syntax`
exercises `create_file` end-to-end through `VerticalSliceRunner` to
`TaskOutcome.COMPLETE`. `test_create_file_rejects_a_path_that_already_exists`
and `test_create_file_builds_a_well_formed_new_file_diff` test
`RepositoryInspector.new_file_patch` directly, the latter round-tripping
the produced diff through `UnifiedDiffParser().parse()` to confirm it is
well-formed with no repair pass needed.

## Maintenance

Adding any future `AgentActionKind` member requires updating, in lockstep:
the `AgentActionKind` enum and its Pydantic model in
`src/apoapsis/agent/actions.py`; the hand-flattened JSON Schema in
`agent_action_schema()` (Ollama's structured-output grammar rejects
Pydantic's discriminated-union `oneOf`, so this cannot be generated
automatically); a new `isinstance` branch in
`BoundedAgentSession._execute` (falls through to an unhandled `TypeError`
otherwise, which is *not* caught by the `AgentInspectionError` handler in
`run()` and would crash the session); and `src/apoapsis/agent/__init__.py`'s
exports. `create_file` followed exactly this checklist and is a template
for the next one.

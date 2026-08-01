# ADR 0058: local tool-template noise normalization

- Status: Accepted
- Date: 2026-07-25

## Context

A live Test Project 3 slice probe against Poolside Laguna S 2.1 GGUF served by
llama.cpp's OpenAI-compatible endpoint no longer returned empty hidden output
after reasoning was disabled, but its first visible action still failed
validation. The model produced a useful `create_file` request, then copied
llama.cpp chat-template tool-call residue into the file content and attached
`command_name: "unit-tests"` and later `reason: "placeholder"`, fields that
belong to other action variants.

The harness was right to reject the response under the strict discriminated
union: unknown authority fields must not be silently accepted. However, this
specific failure is recoverable without granting the model any new capability:
the action discriminator remains `create_file`, the requested path and literal
content remain bounded by the existing `create_file` patch pipeline, and the
extra `command_name` field cannot authorize a check unless the action is
actually `run_check`.

## Decision

`parse_agent_action` now applies one narrow normalization before strict
validation:

- only for `{"action": "create_file", ...}`;
- only when the response contains no fields except `action`, `path`, `content`,
  and known cross-action fields such as `command_name` and `reason`;
- truncates known llama.cpp tool-call closing markers from the literal content;
- removes those irrelevant cross-action fields;
- still rejects unknown fields such as `shell_command`, paths outside the
  repository, existing-file creation, bad patch policy, and every other invalid
  action exactly as before.

This is a parser hardening step, not new model authority. `create_file` still
flows through `RepositoryInspector.new_file_patch`,
`BoundedAgentSession._apply_patch_action`, `PatchPolicyValidator`, Git patch
application, verification, and audit.

## Consequences

Local GGUF models that echo llama.cpp tool-template fragments can make progress
on new-file creation instead of burning turns on an otherwise valid request.
The normalization is intentionally not generalized to arbitrary extra fields or
arbitrary tool markup; additional live failures need their own evidence and
tests before expanding this behavior.

## Verification

Added deterministic coverage in `tests/test_agent_loop.py` proving that the
observed `create_file` plus tool-template residue is accepted after
normalization, while an unrelated `shell_command` field remains rejected.

# Apoapsis 0.8 eval-harness smoke test — `local` lane

- Date: 2026-07-18
- Command: `apoapsis eval download-service --lane local --output-dir .apoapsis-eval/smoke-local`
- Purpose: confirm the new `apoapsis eval` harness (ADR 0008) actually drives
  a real model through a freshly isolated fixture copy end-to-end, not just
  through fake-provider tests. This is a harness smoke test, not the Apoapsis
  0.8 hosted-frontier proof — no `[models.frontier_coder]` was configured, so
  `hybrid`, `forced-escalation`, and `frontier` were not requested.

## Configuration

| Field | Value |
| --- | --- |
| Model | `qwen3-coder-next:q4_K_M` via native loopback Ollama |
| Provider role | `models.frontier` (also used as `local_coder`, since none was separately configured) |
| Execution overlay | lane `local` → `execution.mode=agent`, `execution.route=local_only` |
| Context window | 65,536 tokens (repository default profile) |
| Temperature | 0.0 |
| Task | the fixture's canonical resumable-downloads task (`examples/download-service/README.md`) |
| Fixture copy | `.apoapsis-eval/smoke-local/local/download-service/` (fresh Git repo, isolated from the checked-in fixture) |

## Result

| Field | Value |
| --- | --- |
| Task ID | `TASK-4B9DC90D4857` |
| Outcome | `human_review_required` |
| Stop reason | `agent turn budget exhausted after 12 turns` |
| Calls | 13 (1 specification draft + 12 agent turns) |
| Input tokens | 70,446 |
| Output tokens | 2,018 |
| Cached input tokens | 0 |
| Estimated cost | $0.00 (local, zero configured pricing) |
| Latency | 217.44 s across all calls |
| Patch attempts | 2 |
| Verification runs | 2, both `failed` |
| Files changed | `src/download_service/downloader.py` |
| Audit location | `.apoapsis-eval/smoke-local/local/download-service/.apoapsis/tasks/TASK-4B9DC90D4857/` |

The local agent used its full 12-turn `local_only` budget, attempted two
patches, and left both resumable-download acceptance tests failing. Because
the lane's route is `local_only` (no frontier configured), budget exhaustion
correctly produced `human_review_required` rather than a false completion —
this is the intended fail-closed behavior (ADR 0005/0006), not a harness bug.
This result is consistent with the harder outcome already on record for this
model/task pair in `qwen3-coder-next-smoke.md`, where completion required
tighter, hand-tuned prompting; the eval harness reproduces that difficulty
faithfully rather than papering over it.

## What this does and does not prove

- Proves: `apoapsis eval` builds real provider adapters from the project's
  own `.apoapsis/config.toml`, copies an isolated fixture per lane, runs the
  unmodified `VerticalSliceRunner`, and writes a real audit trail and
  comparison report — end to end, against a real model, with zero code
  changes needed versus the fake-provider path.
- Does not prove: a real hosted-frontier escalation. That still requires a
  real `[models.frontier_coder]` and the `hybrid` / `forced-escalation`
  lanes, per `HANDOFF.md`'s "Known limitations" entry 2.

# Option C: what the pinned Qwen Code 0.21.1 session interface actually offers

Date: 2026-07-30. **No inference was spent.** Every finding below is help text,
bundled documentation, or static symbol inspection of
`apoapsis-qwen-workcell:0.21.1`, run in a `--network none` container with the
probe scripts mounted read-only from
`.apoapsis-eval/slice5c-probe-2026-07-30/`.

## Verdict

**Option B, and by the owner's own decision rule.** Native sessions support
multiple turns and turn-boundary injection, but **not context replacement**.
The rule was: "If they support multiple turns but not context replacement,
choose B."

It is a much stronger B than the fallback the rule implies, and Option A is not
needed.

## What exists

| Capability | Status | Evidence |
|---|---|---|
| Stable session IDs | **Yes** | `qwen sessions list`; JSONL under `~/.qwen/projects/<sanitized-cwd>/chats` |
| Resume a specific session headlessly | **Yes** | `qwen --resume <uuid> -p "<new prompt>"`, documented in `features/headless.md` |
| Continue most recent session | **Yes** | `qwen --continue -p "…"` |
| Multiple prompts into one live session | **Yes** | daemon `POST /session/:id/prompt`, FIFO queue, `--max-pending-prompts-per-session` default 5, overflow `503 prompt_queue_full` |
| Turn-boundary injection of controller text | **Yes** | a resumed `-p` *is* a new user turn; daemon also documents "a mid-turn user message runs first" |
| Daemon session lifecycle | **Yes** | `POST /session`, `POST /session/:id/load`, `POST /session/:id/resume` ("model context is restored on the agent side without UI replay"), `GET /session/:id/transcript` |
| Recap | **Yes** | `POST /session/:id/recap`, wraps core `generateSessionRecap` as a side-query against the fast model; "pollutes neither the main chat history nor the SSE stream" |
| Context observability | **Partial** | `GET /session/:id/context` is listed in the read-only route set but **its payload is undocumented in the bundle**; `context_usage`, `context_limit`, `context_window_size` and `compact_summary` appear as event-field literals in the bundle |
| **Context replacement** | **NO** | `replaceContext`, `setContext`, `forkSession`, `enqueueMidTurnMessage`, `contextUsage`, `turn_complete` are **0 occurrences** in the bundle |
| `resumeSession` over HTTP as an SDK symbol | **Aspirational** | its single occurrence is a roadmap sentence — "without this, no integration can survive a child crash" — not an implementation. The *route* `POST /session/:id/resume` is real; the SDK symbol the owner saw is not the same thing |

## The correction this probe forces

**`DEFAULT_COMPACTION_THRESHOLD = 0.70` is justified in Slice 5's code, commit
message, ADR 0080 and README as "matching Qwen Code's default". That is
false for 0.21.1.**

The real setting is `context.autoCompactThreshold`, **default 0.85**, and it is
not a flat threshold: the docs describe "a three-tier threshold ladder (warn /
auto / hard) computed internally from the model's context window via
`computeThresholds()`", where "on large windows it is the effective trigger
(~85%), while on smaller windows compaction may fire earlier to leave room to
summarize". The setting I matched, `model.chatCompression.contextPercentageThreshold`,
is marked **REMOVED** in this version and "silently ignored (no startup
warning)".

So 0.70 is not a replication of anything. It is a number I chose, documented
with a false provenance. The value may still be defensible; the justification
is not, and it must be rewritten wherever it appears.

Qwen also already does something capsule-shaped after its own compaction:
`model.chatCompression.maxRecentFilesToRetain` (default 5) restores
recently-touched file contents into history, and `maxRecentImagesToRetain`
(default 3) does the same for images.

## What Option B looks like given this

The capsule becomes a **session-handoff document injected as a user turn**, not
a context manager:

1. Apoapsis starts a session, records its id.
2. Qwen owns the agent loop, the tool loop, and **within-session compaction** at
   its own ladder. `context.autoCompactThreshold` becomes an owner-configurable
   pin rather than something Apoapsis reimplements.
3. At a checkpoint, Apoapsis runs the Slice 3/4 admission and readiness path
   unchanged, and injects the capsule with `--resume <id> -p "<capsule + what
   is still outstanding>"`. `features/headless.md`: resume "restores
   conversation history, tool outputs, and chat-compression checkpoints before
   sending the new prompt."
4. `compaction.py`'s two-tier machinery stops being the live context manager. It
   is still the right shape for building the capsule text under a size bound,
   which is a smaller and honest role.

Containment note: this needs **no daemon and no open port**. `--resume` works
through the existing `controller.exec` shape, so ADR 0077's `--network none`
plus controller-owned relay is untouched. The `qwen serve` daemon binds TCP and
is documented as "local-only deployment… containerized deployment (Docker /
k8s) land in a follow-up patch", so it is the wrong tool here even though it is
richer.

## Still unmeasured

- The payload of `GET /session/:id/context` — the one route that might give
  provider-independent context-usage telemetry — is undocumented in the bundle
  and was not called, because calling it requires the daemon.
- Whether `--resume` preserves the yolo execution profile and the 26-tool
  surface across a resumed turn. Slice 2D established that for a fresh `-p`
  only.
- Whether the local server reports `cached_input_tokens` at all. Qwen's
  `features/token-caching.md` says caching applies to "API key users
  (Qwen API key, OpenAI-compatible providers)" and is surfaced via `/stats`,
  and `model.generationConfig.enableCacheControl` exists as a knob — but none of
  that establishes what *this* server reports. Until measured, criterion 5 is
  `NOT_MEASURABLE`.

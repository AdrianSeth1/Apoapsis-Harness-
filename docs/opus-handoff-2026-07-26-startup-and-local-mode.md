# Opus handoff: make Apoapsis easy to launch and test locally

Date: 2026-07-26

## Owner complaint to keep centered

Apoapsis feels buggy and hard to use. The local Laguna mode is especially
annoying because testing it requires too much manual setup: opening Ubuntu,
starting Laguna by hand, then separately opening Apoapsis and selecting the
right project. The desired flow is:

```text
double-click START_APOAPSIS.cmd -> select working folder -> Apoapsis just works
```

Do not solve this by weakening the authority boundary. Models remain untrusted
typed proposers. Apoapsis owns filesystem, shell/process, Git, verification,
workflow transitions, retry ceilings, completion, and audit.

## What changed in this pass

- `START_APOAPSIS.cmd` is now the primary Windows entry point.
  - Accepts a project folder argument, or opens a Windows folder picker.
  - Validates Python, Git, `.git`, and `.apoapsis/config.toml`.
  - Starts configured local coding model services for that selected project.
  - Opens the existing loopback UI for the same project.
- `OPEN_APOAPSIS.cmd` remains as a UI-only fallback.
- `apoapsis.operator_lifecycle` now supports configured loopback
  OpenAI-compatible coding targets, including Laguna via `llama-server`.
  - Checks `/v1/models`.
  - If unavailable, launches only explicit operator setting
    `APOAPSIS_LLAMA_SERVER_COMMAND`.
  - Warms via one minimal `/v1/chat/completions` request.
  - Ignores hosted/non-loopback OpenAI-compatible endpoints.
- UI copy now points users to `START_APOAPSIS.cmd` and project selection.
- README, HANDOFF, and ADR 0062 document the new launch behavior.

Focused verification already run and passed:

```powershell
python -m unittest tests.test_operator_lifecycle tests.test_launcher -v
python -m compileall -q src tests
```

No live Laguna run was performed.

## Audit findings

1. The biggest UX bug was architectural drift: ADR 0060 made Laguna
   `llama-server` the default, but the Start lifecycle was still Ollama-only.
   That guaranteed manual setup friction.
2. The browser UI still cannot select or switch projects by design. That is
   correct for the authority boundary, but it means the launcher/native layer
   must carry project selection well.
3. Initialization is still not one-click. Selecting an uninitialized Git repo
   fails with an instruction to run `apoapsis init`. That is conservative and
   matches the current boundary, but it is still not the owner’s desired
   experience.
4. `llama-server` stop/ownership semantics are not solved. This pass can launch
   an operator-provided command, but `STOP_APOAPSIS.cmd` still only unloads
   Ollama models. A launched `llama-server` remains an operator-owned process.
5. There is no live evidence that `START_APOAPSIS.cmd` successfully launches the
   owner’s actual Laguna command from Windows/WSL and then completes a local
   task.
6. Generated clutter exists in this checkout: `.local_coder]` and ignored
   `__pycache__` folders. Cleanup was attempted but blocked by the local command
   safety policy. Do not mistake those for source.

## Next Opus work

1. On the owner’s Windows machine, configure and test the real Laguna command:

   ```powershell
   setx APOAPSIS_LLAMA_SERVER_COMMAND "<explicit command that starts llama-server on 127.0.0.1:8000>"
   ```

   For WSL, this should probably be an explicit `wsl.exe ...` invocation that
   starts the existing llama.cpp build and GGUF path. Keep it operator-owned;
   do not auto-discover or download models.

2. Run the real happy path:

   ```text
   START_APOAPSIS.cmd -> select initialized test repo -> UI opens -> Local Power on -> submit tiny task -> verify report
   ```

   Record exact evidence in `docs/evaluation/`: process launched or already
   running, endpoint health, model name, task ID, route, Local Power setting,
   verification result, and any failure output.

3. Decide the next UX step with an ADR:
   - Option A: keep batch launcher, add explicit initialization prompt for
     selected uninitialized Git repos.
   - Option B: finish the Tauri desktop shell and move project selection,
     initialization, server lifecycle, and process cleanup there.

   The likely product answer is B, but it requires real Windows/Tauri evidence.

4. Add process ownership for launched `llama-server` only if the lifecycle can
   prove it started that exact process. Do not kill arbitrary port-8000
   processes.

5. Re-run broader tests after the launcher work and existing ADR 0057-0061
   changes:

   ```powershell
   python -m unittest discover -s tests -v
   python -m compileall -q src tests
   git -c core.whitespace=blank-at-eol,space-before-tab,cr-at-eol diff --check
   ```

6. After live startup is reliable, measure Local Power reliability with Laguna.
   Keep held-out oracle data out of prompts and separate fake-provider evidence
   from live local evidence.

## Non-negotiables

- No model gets shell, filesystem, Git, network, workflow-transition,
  verification, completion, retry-limit, or audit authority.
- No browser JavaScript gets arbitrary folder browsing or process-launch
  authority.
- No automatic installs, model downloads, repository creation, or hidden config
  rewriting.
- Preserve user work and the `substrate-v0.1` tag.

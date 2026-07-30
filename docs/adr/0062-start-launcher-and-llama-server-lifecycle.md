# ADR 0062: Start launcher and llama-server lifecycle

- Status: Accepted
- Date: 2026-07-26

## Context

ADR 0060 changed fresh projects to use Poolside Laguna S 2.1 through a local
OpenAI-compatible `llama-server` endpoint on `127.0.0.1:8000`, but
`START_APOAPSIS.cmd` still only understood Ollama. The owner therefore had to
open Ubuntu or another terminal, manually start Laguna, then separately open the
UI with `OPEN_APOAPSIS.cmd` or `apoapsis ui`.

That split made the new local mode hard to test and contradicted the intended
operator flow: start Apoapsis, select a project, and work.

## Decision

`START_APOAPSIS.cmd` is now the primary Windows entry point. It:

- accepts an explicit project folder or opens a Windows folder picker;
- validates Python, Git, the selected Git repository, and `.apoapsis/config.toml`;
- starts the configured loopback local coding service for that selected project;
- opens the existing capability-protected loopback UI for the same project.

`OPEN_APOAPSIS.cmd` remains a UI-only fallback for an already-running model
service.

`apoapsis.operator_lifecycle` now manages two local provider shapes:

- configured loopback Ollama targets, as before;
- configured loopback OpenAI-compatible coding targets such as `llama-server`.

For OpenAI-compatible local targets, the lifecycle manager checks `/v1/models`.
If the endpoint is unavailable, it launches only the explicit
`APOAPSIS_LLAMA_SERVER_COMMAND` supplied by the operator. It never downloads a
model, installs software, initializes a repository, or manages non-loopback
hosted endpoints. After the endpoint is ready, it sends one minimal local
chat-completions request to warm the configured model.

## Consequences

The default Laguna path no longer requires a separate manual server-start step
once the operator has configured `APOAPSIS_LLAMA_SERVER_COMMAND`.

The browser authority boundary is unchanged. Folder selection happens in the
trusted launcher/native layer, and browser JavaScript still cannot browse
arbitrary folders, initialize repositories, start processes, run shell commands,
or select model providers.

Stop behavior remains provider-specific. `STOP_APOAPSIS.cmd` unloads configured
Ollama models through Ollama keep-alive requests. A `llama-server` process
launched by the operator command remains a normal operator-owned local process
in this pass; automatic process ownership/stop semantics are deferred to the
native shell work.

## Verification

Focused deterministic checks:

```powershell
python -m unittest tests.test_operator_lifecycle tests.test_launcher -v
python -m compileall -q src tests
```

These were run on 2026-07-26 and passed. No live Laguna `llama-server` run was
performed in this session.

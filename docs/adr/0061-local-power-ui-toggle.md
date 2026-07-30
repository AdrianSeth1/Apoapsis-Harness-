# ADR 0061: Local Power UI toggle

- Status: Accepted
- Date: 2026-07-26

## Context

ADR 0059 added the Local Power Sandbox as an experimental mode, but activation
still required editing `.apoapsis/config.toml` by hand. That made the feature
easy to miss and easy to misconfigure, especially because enabling it also
requires `execution.mode = "agent"` and cannot be combined with
`route = "frontier_only"`.

## Decision

Add a two-step control on the UI's **Models & environment** page:

- **Turn on Local Power**
- confirmation explaining that the mode is experimental and affects future
  local coding runs
- **Turn off** to return future runs to the strict one-action loop

The browser never writes TOML or chooses arbitrary config. It calls one narrow
server endpoint, `/api/config/local-power`, with `enabled: true|false`.
`ApoapsisUIService.set_local_power_enabled()` edits only:

- `[execution].mode = "agent"`
- `[execution].route`, only from `"frontier_only"` to `"auto"` when enabling
- `[execution.local_power].enabled`

After writing, it reloads the entire config through `ApoapsisConfig`. If
validation fails, the previous file bytes are restored.

## Consequences

Local Power is now discoverable and reversible from the UI without weakening the
authority boundary. Existing execution previews and task start confirmation
continue to show the Local Power warning when the mode is enabled.

The toggle changes future runs only. Existing task records, reports, worktrees,
and audit artifacts are not rewritten.

## Verification

Deterministic coverage:

```powershell
python -m unittest tests.test_ui -v
python -m unittest tests.test_execution_ui -v
```

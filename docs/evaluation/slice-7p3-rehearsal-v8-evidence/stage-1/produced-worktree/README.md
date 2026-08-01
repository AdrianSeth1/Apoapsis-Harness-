# Crisis Atlas

Crisis Atlas is a local-first incident-response workspace for small teams.
It will track incidents, severity, ownership, timeline events, action items,
and a compact operational dashboard without external services or runtime
dependencies.

This repository is an Apoapsis evaluation fixture. The implementation is
deliberately minimal at the initial commit so Architect Mode can decompose and
deliver the product through dependency-ordered vertical slices.

## Product constraints

- Python standard library only.
- All persisted state stays in a user-selected local JSON file.
- Writes must be atomic and malformed data must fail safely.
- The browser UI must work without external assets or network calls.
- Timestamps use explicit UTC ISO-8601 strings.
- Incident history is append-only; corrections are represented by new events.

## Running checks

```powershell
python -m unittest discover -s tests -v
python -m apoapsis verify-web-product --forbid-external-resources --treat-warnings-as-errors
```

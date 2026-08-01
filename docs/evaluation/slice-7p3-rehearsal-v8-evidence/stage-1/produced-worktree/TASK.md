# Crisis Atlas - services slice

Implement the incident and export service layer for Crisis Atlas.

## Required production artifacts

1. `crisis_atlas/services/incident_service.py` defining `IncidentService`.
2. `crisis_atlas/services/export_service.py` defining `ExportService`.

Both modules must live inside the `crisis_atlas` package. A module placed at a
top-level path such as `services/incident_service.py` is outside the declared
package and does not satisfy this task.

## Required behaviour

- `IncidentService` creates incidents and returns them.
- `ExportService` serialises a collection of incidents to deterministic JSON:
  the same input must produce byte-identical output on every call.

## Required tests

Add tests that import and exercise both new services. The repository's
inherited suite does not reach either module; a green inherited suite is
therefore not evidence that this task was completed.

## Constraints

- Python standard library only.
- Do not modify `crisis_atlas/__init__.py` or `tests/test_smoke.py`.

"""A scripted provider for the rehearsal. It cannot reach a model.

`workcell/echo_provider.py` is deterministic but returns its input verbatim,
which is what the ADR 0078 envelope check needs and useless here: the rehearsal
has to produce two *specific* candidate shapes, and producing them is scripting.

Two scripts, both drawn from evidence rather than invented:

* `INCOMPLETE_PROPOSAL` reproduces the historical Crisis Atlas Slice 2 failure
  -- one write to `services/incident_service.py`, no export service, no tests,
  and a summary claiming all three. The summary is deliberately kept, because
  the point of the case is that a confident claim and a true one look identical
  until something reads the change set.
* `COMPLETE_PROPOSAL` is the known-good reference shape: both services at their
  declared package paths plus a test that imports and exercises them.

Neither is model-quality evidence. They are fixtures that make the harness's
own behaviour observable, and the rehearsal verdict says so.

There is no network client here of any kind. The class holds a list of scripted
turns and pops them; a script that runs out raises rather than improvising,
because a provider that invents a turn would be supplying behaviour the
manifest never bound.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class ScriptId(StrEnum):
    INCOMPLETE_PROPOSAL = "incomplete_proposal"
    COMPLETE_PROPOSAL = "complete_proposal"
    OUTPUT_CEILING_TRUNCATION = "output_ceiling_truncation"
    INPUT_CONTEXT_EXHAUSTED = "input_context_exhausted"
    UNCLASSIFIED_STOP_REASON = "unclassified_stop_reason"


class ScriptedTurn(StrictModel):
    """One provider response, with the telemetry a real turn would carry."""

    content: str = Field(min_length=1)
    finish_reason: str = Field(min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    #: Session-level aggregate, when the provider reports one. `None` means the
    #: provider did not report it, which is a fact and not a zero.
    session_total_tokens: int | None = Field(default=None, ge=0)


_INCIDENT_SERVICE_AT_WRONG_PATH = (
    '"""Business operations over the persisted Crisis Atlas incidents."""\n'
    "from __future__ import annotations\n"
    "\n"
    "\n"
    "class IncidentService:\n"
    "    def create_incident(self, title):\n"
    "        return {'title': title}\n"
)

_REFERENCE_FILES = {
    "crisis_atlas/services/__init__.py": '"""Crisis Atlas service layer."""\n',
    "crisis_atlas/services/incident_service.py": (
        '"""Create and return incidents."""\n'
        "\n"
        "\n"
        "class Incident:\n"
        "    def __init__(self, title):\n"
        "        self.title = title\n"
        "\n"
        "\n"
        "class IncidentService:\n"
        "    def create(self, title):\n"
        "        return Incident(title)\n"
    ),
    "crisis_atlas/services/export_service.py": (
        '"""Serialise incidents to deterministic JSON."""\n'
        "\n"
        "import json\n"
        "\n"
        "\n"
        "class ExportService:\n"
        "    def to_json(self, incidents):\n"
        "        return json.dumps(\n"
        "            [item.title for item in incidents], sort_keys=True\n"
        "        )\n"
    ),
    "tests/test_services.py": (
        "import unittest\n"
        "\n"
        "from crisis_atlas.services.export_service import ExportService\n"
        "from crisis_atlas.services.incident_service import IncidentService\n"
        "\n"
        "\n"
        "class ServiceTests(unittest.TestCase):\n"
        "    def test_create_returns_the_incident(self) -> None:\n"
        "        self.assertEqual(IncidentService().create('x').title, 'x')\n"
        "\n"
        "    def test_export_is_deterministic(self) -> None:\n"
        "        incidents = [IncidentService().create('b')]\n"
        "        service = ExportService()\n"
        "        self.assertEqual(\n"
        "            service.to_json(incidents), service.to_json(incidents)\n"
        "        )\n"
    ),
}


def _change_set(summary: str, files: dict[str, str]) -> str:
    return json.dumps(
        {
            "action": "propose_change_set",
            "summary": summary,
            "changes": [
                {"path": path, "operation": "write", "content": body}
                for path, body in sorted(files.items())
            ],
        },
        sort_keys=True,
    )


#: The scripts, keyed by id. Each is a full turn sequence, not a fragment.
SCRIPTS: dict[ScriptId, tuple[ScriptedTurn, ...]] = {
    ScriptId.INCOMPLETE_PROPOSAL: (
        ScriptedTurn(
            # The summary claims three things and the change set contains one.
            # Preserved verbatim in shape from the historical proposal, because
            # the discrepancy is the case.
            content=_change_set(
                "Implement IncidentService and ExportService with unit tests",
                {"services/incident_service.py": _INCIDENT_SERVICE_AT_WRONG_PATH},
            ),
            finish_reason="stop",
            input_tokens=13_562,
            output_tokens=1_127,
            session_total_tokens=14_689,
        ),
    ),
    ScriptId.COMPLETE_PROPOSAL: (
        ScriptedTurn(
            content=_change_set(
                "Add IncidentService and ExportService with tests",
                _REFERENCE_FILES,
            ),
            finish_reason="stop",
            input_tokens=14_002,
            output_tokens=2_310,
            session_total_tokens=16_312,
        ),
    ),
    ScriptId.OUTPUT_CEILING_TRUNCATION: (
        ScriptedTurn(
            content='{"action": "propose_change_set", "summary": "trunc',
            finish_reason="length",
            input_tokens=15_551,
            output_tokens=16_384,
            session_total_tokens=31_935,
        ),
    ),
    ScriptId.INPUT_CONTEXT_EXHAUSTED: (
        ScriptedTurn(
            content="{}",
            finish_reason="length",
            input_tokens=65_536,
            output_tokens=0,
            session_total_tokens=65_536,
        ),
    ),
    ScriptId.UNCLASSIFIED_STOP_REASON: (
        ScriptedTurn(
            content="{}",
            # Not in the classifier's vocabulary. A run that cannot classify
            # its stop reason is INCOMPARABLE, never a pass.
            finish_reason="teapot",
            input_tokens=None,
            output_tokens=None,
            session_total_tokens=None,
        ),
    ),
}


def script_digest() -> str:
    """One digest over every script, for the manifest to bind.

    Covers the scripts rather than the module, so a comment change does not
    invalidate a lock while a changed candidate byte does.
    """

    payload = {
        str(script): [turn.model_dump(mode="json") for turn in turns]
        for script, turns in sorted(SCRIPTS.items())
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ScriptExhausted(RuntimeError):
    pass


class FakePilotProvider:
    """Serves a script. Holds no client and can reach nothing.

    `requests` records every call so the rehearsal can prove request counts and
    match them against relay traffic: a turn that never crossed the relay is a
    turn that bypassed containment.
    """

    #: Structural guarantee, asserted by a test: this provider has no transport.
    reaches_network = False

    def __init__(self, script: ScriptId) -> None:
        self.script = script
        self._remaining = list(SCRIPTS[script])
        self.requests: list[dict[str, object]] = []

    def complete(self, prompt: str) -> ScriptedTurn:
        self.requests.append({"index": len(self.requests), "prompt_chars": len(prompt)})
        if not self._remaining:
            raise ScriptExhausted(
                f"script {self.script} has no turn left. A provider that "
                "improvised here would supply behaviour the manifest never "
                "bound."
            )
        return self._remaining.pop(0)

    @property
    def request_count(self) -> int:
        return len(self.requests)

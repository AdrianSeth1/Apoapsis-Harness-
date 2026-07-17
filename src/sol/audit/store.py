from __future__ import annotations

import json
import os
import tempfile
import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sol.context.compiler import ContextPackage
from sol.models.base import ModelRequest, ModelResponse
from sol.models.telemetry import ProviderCallTelemetry
from sol.specification.schema import StrictModel


class AuditArtifact(StrictModel):
    kind: str
    path: str


class TaskAuditStore:
    def __init__(self, project_root: str | Path, task_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.task_id = task_id
        self.root = self.project_root / ".sol" / "tasks" / task_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._artifacts: list[AuditArtifact] = []

    def write_call_package(
        self,
        call_number: int,
        request: ModelRequest,
        prompt: str,
        context: ContextPackage,
    ) -> list[AuditArtifact]:
        prefix = f"call-{call_number:03d}"
        context_path = self.write_json(
            f"{prefix}-context.json", context, kind="context_package"
        )
        payload = {
            "schema_version": "1.0",
            "call_number": call_number,
            "model_request": request.model_dump(mode="json"),
            "prompt": prompt,
            "context_artifact": context_path.path,
            "context_sha256": context.context_sha256,
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        payload["request_package_sha256"] = hashlib.sha256(canonical).hexdigest()
        request_path = self.write_json(
            f"{prefix}-request.json", payload, kind="model_request"
        )
        return [context_path, request_path]

    def write_call_result(
        self,
        call_number: int,
        response: ModelResponse,
        telemetry: ProviderCallTelemetry,
    ) -> list[AuditArtifact]:
        prefix = f"call-{call_number:03d}"
        return [
            self.write_json(
                f"{prefix}-response.json", response, kind="model_response"
            ),
            self.write_json(
                f"{prefix}-telemetry.json", telemetry, kind="provider_telemetry"
            ),
        ]

    def write_json(
        self, filename: str, value: BaseModel | dict[str, Any] | list[Any], *, kind: str
    ) -> AuditArtifact:
        if isinstance(value, BaseModel):
            serializable: Any = value.model_dump(mode="json")
        else:
            serializable = value
        text = json.dumps(serializable, indent=2, sort_keys=True) + "\n"
        return self._write(filename, text, kind)

    def write_text(self, filename: str, value: str, *, kind: str) -> AuditArtifact:
        return self._write(filename, value, kind)

    def artifacts(self) -> list[AuditArtifact]:
        return list(self._artifacts)

    def _write(self, filename: str, value: str, kind: str) -> AuditArtifact:
        if Path(filename).name != filename:
            raise ValueError("audit filename must not contain a directory")
        destination = self.root / filename
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", dir=self.root, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
        relative = destination.relative_to(self.project_root).as_posix()
        artifact = AuditArtifact(kind=kind, path=relative)
        self._artifacts.append(artifact)
        return artifact

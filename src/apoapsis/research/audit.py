from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ResearchAuditStore:
    def __init__(self, project_root: str | Path, task_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / ".apoapsis" / "tasks" / task_id / "research"
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, filename: str, value: BaseModel | dict[str, Any]) -> str:
        serializable = (
            value.model_dump(mode="json")
            if isinstance(value, BaseModel)
            else value
        )
        return self._write(
            filename, json.dumps(serializable, indent=2, sort_keys=True) + "\n"
        )

    def write_jsonl(
        self,
        filename: str,
        values: list[BaseModel | dict[str, Any]],
    ) -> str:
        lines = []
        for value in values:
            serializable = (
                value.model_dump(mode="json")
                if isinstance(value, BaseModel)
                else value
            )
            lines.append(json.dumps(serializable, sort_keys=True))
        return self._write(filename, "\n".join(lines) + ("\n" if lines else ""))

    def write_text(self, filename: str, value: str) -> str:
        return self._write(filename, value)

    def _write(self, filename: str, value: str) -> str:
        if Path(filename).name != filename:
            raise ValueError("research audit filename must be a basename")
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
        return destination.relative_to(self.project_root).as_posix()


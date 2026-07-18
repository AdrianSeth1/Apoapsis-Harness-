from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from apoapsis.execution.backend import ExecutionContext, RawCommandOutcome
from apoapsis.verification.results import VerificationStatus

if TYPE_CHECKING:
    from apoapsis.verification.runner import VerificationCommand


class HostExecutionBackend:
    """The pre-0.9 behavior: run configured commands directly on the host.

    Kept only as an explicitly selected compatibility backend. Every result
    is reported with `sandboxed: False` — it provides no process isolation,
    network denial, or resource limits beyond the process timeout.
    """

    backend_name = "host"

    def prepare(
        self, project_root: Path, task_id: str, attempt: int
    ) -> ExecutionContext:
        del task_id, attempt
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ValueError(f"project root does not exist: {root}")
        return ExecutionContext(root=root, display_root=str(root), extra={})

    def run_command(
        self,
        context: ExecutionContext,
        command: "VerificationCommand",
        *,
        environment: dict[str, str],
    ) -> RawCommandOutcome:
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        exit_code: int | None = None
        stdout = ""
        stderr = ""
        status = VerificationStatus.ERROR
        try:
            completed = subprocess.run(
                command.argv,
                cwd=context.root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=command.timeout_seconds,
                shell=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            status = (
                VerificationStatus.PASSED
                if completed.returncode == 0
                else VerificationStatus.FAILED
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._to_text(exc.stdout)
            stderr = self._to_text(exc.stderr)
            status = VerificationStatus.TIMED_OUT
        except OSError as exc:
            stderr = str(exc)
            status = VerificationStatus.ERROR
        duration = time.monotonic() - started_clock
        return RawCommandOutcome(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_seconds=duration,
            backend=self.backend_name,
            backend_metadata={"sandboxed": False},
        )

    def finalize(self, context: ExecutionContext) -> list[str]:
        del context
        return []

    @staticmethod
    def _to_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

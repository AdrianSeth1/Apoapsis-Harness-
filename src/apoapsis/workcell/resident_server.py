"""Stop a loopback model server that is holding weights, by proving which one.

`operator_lifecycle.stop_local_models` used to report a running llama-server and
refuse to touch it. Its reasoning was right and is worth preserving verbatim:
Apoapsis starts that server through an operator-supplied command line that may
cross a process boundary it cannot see through -- `wsl.exe ...` returns the PID
of `wsl.exe`, not of the server inside the distribution -- so killing *by port*
means killing whatever a stranger happened to be running there.

The consequence was that "Apoapsis model memory has been released" left the
single largest consumer untouched. A 27B model at Q4_K_M with all layers
offloaded holds about 20 GB of a 24 GB card, and the operator's only remedy was
to find and kill a process by hand, inside WSL, that Apoapsis had started for
them.

What resolves the objection is not more force, it is **identity**. Three facts,
each checked rather than assumed:

1. the endpoint is one this project is *configured* to use, and it is loopback;
2. that server, asked directly, reports the model file it currently has open
   (`GET /props` -> `model_path`);
3. the process signalled is the one whose *own command line* names that exact
   file.

That is the same discipline `workcell/product.py` already applies before a
sandbox run, through the same shell tool, and it is not a guess about identity
-- two copies of the same weights cannot both be resident, so a process whose
command line names this model *is* the one serving it. A llama-server holding
different weights is never matched and never signalled.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: The shell tool that does the matching and signalling. Shared with
#: `workcell/product.py` deliberately: one implementation of "which process is
#: serving this model file", used by both callers.
RESIDENT_SERVER_TOOL = "tools/resident_model_server.sh"

_WSL_DISTRIBUTION = "Ubuntu-24.04"


class StoppedServer(StrictModel):
    """One server that was asked to stop, and what it was."""

    base_url: str
    model_path: str | None = None
    pids: list[int] = Field(default_factory=list)
    command_line: str = ""
    stopped: bool = False
    #: Why nothing was stopped. Empty when something was.
    reason: str = ""


def resident_model_path(base_url: str, *, timeout_seconds: float = 5.0) -> str | None:
    """Ask a llama-server which model file it currently holds.

    `None` for anything that is not a llama-server answering right now --
    unreachable, a different OpenAI-compatible implementation, or a build whose
    `/props` omits the path. In every one of those cases the caller has no
    identity proof and must not signal anything.
    """

    request = urllib.request.Request(
        f"{base_url.rstrip('/').removesuffix('/v1')}/props",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    path = payload.get("model_path") if isinstance(payload, dict) else None
    return path if isinstance(path, str) and path.strip() else None


def _tool_command(harness_root: Path, mode: str, target: str) -> list[str]:
    script = harness_root / RESIDENT_SERVER_TOOL
    if os.name == "nt":
        from apoapsis.workcell.product import _wsl_path

        return [
            "wsl.exe",
            "-d",
            _WSL_DISTRIBUTION,
            "--",
            "bash",
            _wsl_path(script),
            mode,
            target,
        ]
    return ["bash", str(script), mode, target]


def stop_resident_server(
    harness_root: Path | str,
    base_url: str,
    *,
    timeout_seconds: float = 180.0,
    runner=subprocess.run,
) -> StoppedServer:
    """Stop the server at `base_url`, if its identity can be proven.

    Never raises. A caller here is a shutdown path, and a shutdown that fails
    loudly because it could not reach a service that is already gone is worse
    than one that reports what it found.
    """

    model_path = resident_model_path(base_url)
    if model_path is None:
        return StoppedServer(
            base_url=base_url,
            reason=(
                "the endpoint did not report a model file, so no process could "
                "be identified; nothing was signalled"
            ),
        )

    root = Path(harness_root)
    if not (root / RESIDENT_SERVER_TOOL).is_file():
        return StoppedServer(
            base_url=base_url,
            model_path=model_path,
            reason=f"{RESIDENT_SERVER_TOOL} is not present in the Apoapsis installation",
        )

    try:
        completed = runner(
            _tool_command(root, "stop", model_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return StoppedServer(
            base_url=base_url,
            model_path=model_path,
            reason=f"could not run the resident-server tool: {exc}",
        )

    # The tool prints "<pid>\t<command line>" per match and exits 0 either way.
    # No matches means the model file is held by nothing this host can see --
    # which is the correct outcome for an endpoint served from elsewhere.
    entries = [
        line.split("\t", 1)
        for line in (completed.stdout or "").splitlines()
        if "\t" in line
    ]
    if not entries:
        return StoppedServer(
            base_url=base_url,
            model_path=model_path,
            reason=(
                "no local process names this model file, so the endpoint is "
                "served from outside this machine and was left alone"
            ),
        )

    pids: list[int] = []
    for raw_pid, _command in entries:
        try:
            pids.append(int(raw_pid.strip()))
        except ValueError:
            continue
    return StoppedServer(
        base_url=base_url,
        model_path=model_path,
        pids=pids,
        command_line=entries[0][1].strip(),
        stopped=True,
    )


class HostMemoryReport(StrictModel):
    """What is still held after a model server has been stopped, and why.

    Stopping the server returns its resident set inside the distribution
    immediately. It does *not* return that memory to Windows, and the operator
    watching Task Manager sees a number that barely moved. Two causes, both
    worth naming rather than leaving as a mystery:

    * reading a multi-gigabyte GGUF fills the page cache, which is clean,
      reclaimable, and still counted as used by the VM;
    * WSL2 only hands freed pages back to Windows when `autoMemoryReclaim` is
      configured, and it is off by default.

    Apoapsis cannot fix either one. Dropping caches needs root inside the
    distribution (`sudo` there requires a password), and `wsl --shutdown` would
    reclaim everything at the cost of stopping Docker Desktop's backend and any
    other distribution work. So this reports, precisely, and hands the operator
    the one-line remedy.
    """

    distribution_used_mb: int | None = None
    distribution_cache_mb: int | None = None
    auto_memory_reclaim: str | None = None
    remedy: str = ""


_WSLCONFIG_HINT = (
    "WSL2 is not configured to return freed memory to Windows. Add this to "
    "%USERPROFILE%\\.wslconfig under [wsl2] and restart WSL once:\n"
    "    autoMemoryReclaim=gradual\n"
    "Until then the utility VM keeps pages it is no longer using -- including "
    "the page cache from reading the model file. `wsl --shutdown` reclaims it "
    "immediately, but also stops Docker Desktop's backend, so Apoapsis will "
    "not run it for you."
)


def host_memory_report(*, runner=subprocess.run) -> HostMemoryReport:
    """Measure what the distribution still holds, and why Windows sees it."""

    if os.name != "nt":
        return HostMemoryReport()

    report = HostMemoryReport()
    try:
        completed = runner(
            ["wsl.exe", "-d", _WSL_DISTRIBUTION, "--", "free", "-m"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        for line in (completed.stdout or "").splitlines():
            if not line.lower().startswith("mem:"):
                continue
            parts = line.split()
            # total used free shared buff/cache available
            if len(parts) >= 7:
                report.distribution_used_mb = int(parts[2])
                report.distribution_cache_mb = int(parts[5])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    try:
        config = Path.home() / ".wslconfig"
        text = config.read_text(encoding="utf-8", errors="replace") if config.is_file() else ""
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "automemoryreclaim":
                report.auto_memory_reclaim = value.strip()
    except OSError:
        pass

    if report.auto_memory_reclaim is None:
        report.remedy = _WSLCONFIG_HINT
    return report


__all__ = [
    "RESIDENT_SERVER_TOOL",
    "HostMemoryReport",
    "StoppedServer",
    "host_memory_report",
    "resident_model_path",
    "stop_resident_server",
]

"""Where the Unix-socket relay can actually work, and where it cannot.

This module exists because the obvious assumption is false. A Unix domain
socket created by a process on a Windows host **cannot** be bind-mounted into a
Docker Desktop Linux container. Windows has had `AF_UNIX` since Windows 10
1803, so the socket file is real and `bind()` succeeds — but Docker Desktop
shares Windows paths into the Linux VM over a filesystem (9p/gRPC-FUSE/virtiofs
depending on the backend) that does not carry socket inodes. The container sees
an ordinary file, or nothing, and `connect()` fails.

The failure is quiet and late: everything up to the first model request looks
fine. So this classifies the situation up front and refuses the unsupported
ones with the actual fix, rather than letting a run reach preflight and produce
a confusing `ECONNREFUSED`.

The same trap exists one level down. Inside WSL2, a path under `/mnt/c` is
DrvFs — the Windows filesystem again — and has the same problem. A socket must
live on the distro's own ext4.
"""

from __future__ import annotations

import os
import platform
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class HostPlatform(StrEnum):
    LINUX = "linux"
    WSL2 = "wsl2"
    WINDOWS = "windows"
    MACOS = "macos"
    UNKNOWN = "unknown"


class SocketSupport(StrEnum):
    #: The socket can be created here and mounted into the container.
    SUPPORTED = "supported"
    #: The platform works, but this particular path will not.
    UNSUPPORTED_PATH = "unsupported_path"
    #: No socket-based relay is possible from this host at all.
    UNSUPPORTED_PLATFORM = "unsupported_platform"


class PlatformAssessment(StrictModel):
    host_platform: HostPlatform
    support: SocketSupport
    socket_path: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    #: Concrete next steps, not "consult the documentation".
    remedies: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.support == SocketSupport.SUPPORTED


#: WSL2 mounts the Windows drives here by default. A socket on DrvFs is the
#: same failure as a socket on the Windows host.
_WINDOWS_INTEROP_PREFIXES: tuple[str, ...] = ("/mnt/c/", "/mnt/d/", "/mnt/e/")


def detect_host_platform() -> HostPlatform:
    if sys.platform == "win32":
        return HostPlatform.WINDOWS
    if sys.platform == "darwin":
        return HostPlatform.MACOS
    if sys.platform.startswith("linux"):
        release = platform.uname().release.lower()
        if "microsoft" in release or "wsl" in release:
            return HostPlatform.WSL2
        return HostPlatform.LINUX
    return HostPlatform.UNKNOWN


def assess_socket_support(
    socket_path: str, *, host_platform: HostPlatform | None = None
) -> PlatformAssessment:
    """Decide whether a relay socket at this path can serve the container."""

    detected = host_platform or detect_host_platform()
    normalised = socket_path.replace("\\", "/")

    if detected == HostPlatform.WINDOWS:
        return PlatformAssessment(
            host_platform=detected,
            support=SocketSupport.UNSUPPORTED_PLATFORM,
            socket_path=socket_path,
            detail=(
                "a Unix domain socket created on a Windows host cannot be "
                "bind-mounted into a Docker Desktop Linux container. Windows "
                "supports AF_UNIX, so the bind succeeds and the mount appears to "
                "work, but the shared filesystem does not carry socket inodes "
                "and the container's connect() fails at the first model request."
            ),
            remedies=[
                "Run the Apoapsis controller inside a WSL2 distribution and put "
                "the socket on the distro's own filesystem (for example "
                "/run/apoapsis/model.sock or ~/.apoapsis/run/model.sock), not "
                "under /mnt/c.",
                "Or run the controller on a Linux host or VM with a native "
                "Docker engine.",
                "Do not substitute a TCP port on the host: that would require "
                "giving the workcell a network route, which ADR 0077 forbids.",
            ],
        )

    if detected == HostPlatform.WSL2 and normalised.startswith(
        _WINDOWS_INTEROP_PREFIXES
    ):
        return PlatformAssessment(
            host_platform=detected,
            support=SocketSupport.UNSUPPORTED_PATH,
            socket_path=socket_path,
            detail=(
                f"{socket_path} is on a Windows drive mounted into WSL2 (DrvFs). "
                "That is the Windows filesystem again, and it cannot carry a "
                "socket inode into the container."
            ),
            remedies=[
                "Move the socket onto the distribution's own ext4 filesystem, "
                "for example /run/apoapsis/model.sock.",
            ],
        )

    if detected in {HostPlatform.LINUX, HostPlatform.WSL2, HostPlatform.MACOS}:
        return PlatformAssessment(
            host_platform=detected,
            support=SocketSupport.SUPPORTED,
            socket_path=socket_path,
            detail=(
                f"{detected.value} can create a Unix domain socket at "
                f"{socket_path} and share it with the container"
            ),
        )

    return PlatformAssessment(
        host_platform=detected,
        support=SocketSupport.UNSUPPORTED_PLATFORM,
        socket_path=socket_path,
        detail=f"socket support on {sys.platform!r} has not been established",
        remedies=["Run the controller on Linux, WSL2, or macOS."],
    )


#: Nothing but the socket may live in the mounted directory. Mounting a broad
#: writable host directory would hand the workcell a channel the relay does not
#: mediate, which is the whole thing the relay exists to prevent.
def prepare_socket_directory(socket_path: str) -> Path:
    """Create a dedicated, empty directory for exactly one socket.

    Raises if the directory already holds anything else, rather than mounting a
    directory whose contents nobody has looked at.
    """

    path = Path(socket_path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    residue = [item.name for item in directory.iterdir() if item.name != path.name]
    if residue:
        raise ValueError(
            f"the relay socket directory {directory} is not dedicated; it also "
            f"contains {', '.join(sorted(residue))}. Mounting it would share "
            "those with the workcell over a channel the relay does not mediate."
        )
    # A socket left behind by a crashed controller will make bind() fail with
    # EADDRINUSE even though nothing is listening.
    if path.exists():
        path.unlink()
    try:
        os.chmod(directory, 0o770)
    except OSError:
        # Best effort: on some filesystems this is not settable, and the
        # dedicated-directory check above is the load-bearing control.
        pass
    return directory

"""Build the Capability Sandbox controller image before a slice needs it.

The image is tagged by *harness commit* and built `--no-cache` (the build
script explains why: a cached LABEL layer would carry another context's
digest). Both of those are correct and together they have a consequence nobody
chose: every commit to Apoapsis invalidates the tag, so the next slice an
operator starts pays a full image build inside its own critical path, before
any model work, with nothing on screen but a spinner.

Observed on the owner's machine while implementing MH-9: twenty-four
`apoapsis-product-controller` tags, one per commit, 424 MB each, and the tag
for the then-current HEAD absent -- so the next slice would have paid a build.
Measured on that machine on 2026-08-03, the build takes **34 s**. Not the
catastrophe the review's "a live sandbox run may docker-build the controller
image before any model work" implies, and not nothing either: it is half a
minute of an unmoving spinner before a single token is generated, once per
harness commit, at the exact moment an operator is waiting to see whether
their slice works.

So this module does two things and refuses to do a third:

**Warm.** `prebuild_controller_image` builds the image for the current commit
if it is absent, and does nothing if it is present. Called at launcher start,
that half minute moves from "the operator clicked Run and nothing happened" to
"the app took a moment to open" -- a wait people already understand, and one
that is not blocking a decision.

**Report what is stale.** `stale_controller_images` lists the per-commit tags
that are not the current one, so an operator can see what accumulated.

**It does not delete anything on its own.** Reclaiming disk is an operator's
decision, not a side effect of starting an app -- and an image the harness
considers stale may be exactly the one someone is mid-way through comparing
against. `prune_controller_images` exists, takes an explicit list, and is
never called automatically.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: The repository holding the product controller images. Must agree with
#: `tools/run_capability_sandbox_task.sh`, which computes the same tag in
#: shell before deciding whether to build -- a drift-guard test asserts the two
#: still agree, because a disagreement here does not fail, it silently builds a
#: second copy of an image that already exists under another name.
CONTROLLER_IMAGE_REPOSITORY = "apoapsis-product-controller"

#: How much of the commit sha the tag carries. Twelve, matching the shell.
CONTROLLER_TAG_COMMIT_CHARS = 12

_WSL_DISTRIBUTION = "Ubuntu-24.04"


class ControllerImageError(RuntimeError):
    """The image could not be inspected or built."""


class ControllerImageStatus(StrictModel):
    """What the warm attempt found and did.

    Deliberately reports `attempted=False` with a reason rather than raising
    when Docker is simply not there: an operator without Docker still uses the
    UI, the intake flow and the review surfaces, and a launcher that refuses to
    start because a sandbox image could not be warmed would be trading a large
    capability for a small one.
    """

    tag: str
    attempted: bool
    built: bool = False
    already_present: bool = False
    #: Why nothing was attempted, or why an attempt failed. Empty on success.
    reason: str = ""
    duration_seconds: float | None = None


def controller_image_tag(harness_root: Path | str, commit: str | None = None) -> str:
    """The image tag for one harness commit.

    `commit` defaults to the harness's current `HEAD`, which is what a slice
    launched now would use.
    """

    if commit is None:
        commit = _git_head(Path(harness_root))
    return f"{CONTROLLER_IMAGE_REPOSITORY}:{commit[:CONTROLLER_TAG_COMMIT_CHARS]}"


def _git_head(harness_root: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed executable and argv
        ["git", "-C", str(harness_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        raise ControllerImageError(
            "the Apoapsis installation folder is not a Git repository: "
            + (completed.stderr or "").strip()
        )
    return completed.stdout.strip()


def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed executable, argv from callers
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def docker_available() -> bool:
    """Whether a Docker daemon answers at all.

    `docker version` rather than `docker --version`: the second reports the
    client and succeeds with no daemon running, which is exactly the state
    this needs to detect.
    """

    try:
        return _docker("version", "--format", "{{.Server.Version}}", timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def image_present(tag: str) -> bool:
    try:
        return _docker("image", "inspect", tag, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def list_controller_images() -> list[str]:
    """Every tag in the controller repository, newest first."""

    try:
        completed = _docker(
            "images",
            CONTROLLER_IMAGE_REPOSITORY,
            "--format",
            "{{.Repository}}:{{.Tag}}",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def stale_controller_images(current_tag: str, *, keep: int = 2) -> list[str]:
    """Controller images that are not the current one, oldest last.

    `keep` retains the most recent few besides the current tag, because the
    common reason to want an older one is comparing a result against the
    harness that produced it, and that is nearly always a recent harness.
    Returns names only; deleting is a separate, explicit call.
    """

    images = [tag for tag in list_controller_images() if tag != current_tag]
    return images[keep:]


def prune_controller_images(tags: list[str]) -> dict[str, str]:
    """Remove exactly the tags given. Never chooses them itself.

    Returns tag -> outcome, so a caller can report what actually happened
    rather than assuming. An image still referenced by a container fails to
    remove and says so; that is information, not an error to raise on.
    """

    outcomes: dict[str, str] = {}
    for tag in tags:
        if not tag.startswith(f"{CONTROLLER_IMAGE_REPOSITORY}:"):
            # Refuses to remove anything outside its own repository even when
            # asked. This function takes a list from a caller, and a caller
            # that computed that list wrongly should not be able to delete an
            # unrelated image through it.
            outcomes[tag] = "refused: not an Apoapsis controller image"
            continue
        try:
            completed = _docker("image", "rm", tag, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            outcomes[tag] = f"error: {exc}"
            continue
        outcomes[tag] = (
            "removed" if completed.returncode == 0
            else f"kept: {(completed.stderr or '').strip()}"
        )
    return outcomes


def _build_command(harness_root: Path, commit: str, tag: str) -> list[str]:
    """The argv that builds the image, for this host.

    On Windows the build runs inside the WSL distribution that owns the Docker
    socket and the ext4 runtime, exactly as `product.py` launches the slice
    itself; elsewhere it is a direct `bash`.
    """

    script = harness_root / "docker" / "pilot-controller" / "build.sh"
    if os.name == "nt":
        from apoapsis.workcell.product import _wsl_path

        return [
            "wsl.exe",
            "-d",
            _WSL_DISTRIBUTION,
            "--",
            "bash",
            _wsl_path(script),
            commit,
            tag,
            _wsl_path(harness_root),
        ]
    return ["bash", str(script), commit, tag, str(harness_root)]


def prebuild_controller_image(
    harness_root: Path | str,
    *,
    timeout_seconds: float = 1_800.0,
    runner=subprocess.run,
) -> ControllerImageStatus:
    """Warm the controller image for the current harness commit.

    Idempotent and non-fatal. Every failure path returns a status carrying the
    reason instead of raising, because the only caller is a launcher whose job
    is to start an app the operator can use with or without a sandbox.
    """

    root = Path(harness_root)
    try:
        tag = controller_image_tag(root)
    except ControllerImageError as exc:
        return ControllerImageStatus(tag="", attempted=False, reason=str(exc))

    if not docker_available():
        return ControllerImageStatus(
            tag=tag,
            attempted=False,
            reason=(
                "Docker is not running, so the Capability Sandbox image was "
                "not prepared. Start Docker Desktop before running a plan "
                "slice; everything else in Apoapsis works without it."
            ),
        )
    if image_present(tag):
        return ControllerImageStatus(tag=tag, attempted=False, already_present=True)

    commit = _git_head(root)
    import time

    started = time.monotonic()
    try:
        completed = runner(
            _build_command(root, commit, tag),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ControllerImageStatus(
            tag=tag,
            attempted=True,
            reason=(
                f"the controller image build did not finish within "
                f"{timeout_seconds:.0f}s"
            ),
            duration_seconds=round(time.monotonic() - started, 1),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ControllerImageStatus(
            tag=tag, attempted=True, reason=f"the controller image build failed: {exc}"
        )
    duration = round(time.monotonic() - started, 1)
    if completed.returncode != 0:
        return ControllerImageStatus(
            tag=tag,
            attempted=True,
            reason=(
                "the controller image build failed: "
                + ((completed.stderr or completed.stdout or "").strip()[-400:])
            ),
            duration_seconds=duration,
        )
    return ControllerImageStatus(
        tag=tag, attempted=True, built=True, duration_seconds=duration
    )


__all__ = [
    "CONTROLLER_IMAGE_REPOSITORY",
    "CONTROLLER_TAG_COMMIT_CHARS",
    "ControllerImageError",
    "ControllerImageStatus",
    "controller_image_tag",
    "docker_available",
    "image_present",
    "list_controller_images",
    "prebuild_controller_image",
    "prune_controller_images",
    "stale_controller_images",
]

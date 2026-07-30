"""Build the disposable clone the workcell mounts read-write at `/workspace`.

The Slice 2 live gate failed its first containment run for a reason worth
keeping in front of us: the operator had to strip project `.apoapsis` state and
a Git remote *by hand* before the 22 probes would pass. A sanitation step that
lives in an operator's memory is a sanitation step that will eventually be
skipped, and the probe it fails is `git-remote-sanitized` -- the one that makes
"Git inside the workcell is safe" true rather than hopeful.

So the clone is built by code, audited by code, and the audit is fail-closed.
Three properties are non-negotiable and each maps onto a containment probe:

* **No remotes.** Git is only safe inside the workcell because there is nowhere
  to push. (`git-remote-sanitized`)
* **No `.apoapsis`.** Apoapsis's own state must not sit inside the tree the
  model can rewrite. (`apoapsis-checkout-absent`)
* **The task artifact lives outside the clone.** Otherwise the approved task
  appears in the delivered project tree and in the computed delta.
  (`task-artifact-outside-workspace`)

Owner history is dropped rather than sanitised: the clone is made shallow, so
the disposable environment never contains commits whose contents nobody
reviewed for secrets.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Sequence

from pydantic import Field

from apoapsis.specification.schema import StrictModel

#: Exact directory or file names that must never exist in the clone. Apoapsis
#: state first, then the credential material a developer checkout accumulates.
SANITIZED_NAMES: tuple[str, ...] = (
    ".apoapsis",
    ".apoapsis-eval",
    ".aws",
    ".docker",
    ".git-credentials",
    ".gitconfig",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ssh",
)

#: Prefixes that must never survive. `.env` alone is covered by the prefix, and
#: so are `.env.local`, `.env.production`, and every other variant that a
#: fixed-name list would miss.
SANITIZED_PREFIXES: tuple[str, ...] = (".env",)

#: Local Git configuration keys that can reach the network or an identity.
#: Removed rather than overwritten, because an unset key cannot be misread.
SANITIZED_GIT_CONFIG_PREFIXES: tuple[str, ...] = (
    "credential.",
    "http.",
    "https.",
    "remote.",
    "url.",
)

GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess]


class CloneSanitizationError(RuntimeError):
    """Raised when a clone could not be produced in a state fit to mount."""


class SanitizedCloneReport(StrictModel):
    """What clone creation actually did, not what it intended to do."""

    schema_version: str = "1.0"
    source_repository: str = Field(min_length=1)
    clone_path: str = Field(min_length=1)
    #: Commit of the *source* repository the clone was made from. This is the
    #: value that goes into `WorkcellPin.seed_commit`, and both paired arms must
    #: share it.
    seed_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    #: HEAD of the clone itself. Differs from `seed_commit` only when something
    #: had to be removed, because the removal is committed rather than left as
    #: an unexplained dirty worktree.
    clone_head_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    removed_remotes: list[str] = Field(default_factory=list)
    removed_paths: list[str] = Field(default_factory=list)
    removed_git_config_keys: list[str] = Field(default_factory=list)
    shallow: bool = False
    task_artifact_path: str = Field(min_length=1)
    task_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Anything the post-build audit could still see. Non-empty means the clone
    #: must not be mounted.
    residual_findings: list[str] = Field(default_factory=list)
    sanitized: bool = False


def plan_removals(entries: Iterable[str]) -> list[str]:
    """Which top-level entries must not survive into the workcell.

    Pure, so the policy can be tested without a repository. Returned sorted
    because the report is evidence and evidence should not reorder itself
    between runs.
    """

    doomed = {
        name
        for name in entries
        if name in SANITIZED_NAMES
        or any(name.startswith(prefix) for prefix in SANITIZED_PREFIXES)
    }
    return sorted(doomed)


def plan_git_config_removals(keys: Iterable[str]) -> list[str]:
    """Local Git config keys that could reach a network or an identity."""

    return sorted(
        {
            key
            for key in keys
            if any(key.startswith(prefix) for prefix in SANITIZED_GIT_CONFIG_PREFIXES)
        }
    )


def audit_clone(
    *,
    remotes: Sequence[str],
    entries: Sequence[str],
    git_config_keys: Sequence[str],
    task_artifact_path: str,
    clone_path: str,
) -> list[str]:
    """Re-inspect a finished clone and report anything still wrong.

    Deliberately independent of `create_sanitized_clone`: it re-derives the
    findings from observed state rather than trusting the builder's own record.
    A builder that silently skipped a step has to be caught by something that
    did not run the step.
    """

    findings: list[str] = []
    if remotes:
        findings.append(
            "the clone still has Git remote(s) "
            + ", ".join(sorted(remotes))
            + "; Git is only safe in the workcell because there is nowhere to push"
        )
    surviving = plan_removals(entries)
    if surviving:
        findings.append(
            "these entries must not be inside the workcell tree: "
            + ", ".join(surviving)
        )
    leftover_config = plan_git_config_removals(git_config_keys)
    if leftover_config:
        findings.append(
            "local Git configuration still names a network or identity: "
            + ", ".join(leftover_config)
        )
    normalised_clone = clone_path.replace("\\", "/").rstrip("/")
    normalised_artifact = task_artifact_path.replace("\\", "/")
    if normalised_artifact.startswith(normalised_clone + "/"):
        findings.append(
            "the task artifact is inside the clone, so the approved task would "
            "appear in the delivered project tree and in the computed delta"
        )
    return findings


def _default_git_runner(
    argv: Sequence[str], cwd: Path
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        shell=False,
        check=False,
    )


def parse_workspace_owner(owner: str) -> tuple[int, int]:
    """Split a `uid:gid` string, refusing anything else.

    Names are not accepted on purpose: the workcell user exists only inside the
    image, so a name resolved against the controller's passwd file would silently
    chown the tree to a different account than the one that will read it.
    """

    uid, separator, gid = owner.partition(":")
    if not separator or not uid.isdigit() or not gid.isdigit():
        raise CloneSanitizationError(
            f"the workspace owner must be numeric uid:gid, not {owner!r}"
        )
    return int(uid), int(gid)


def _chown_tree(root: Path, owner: str) -> None:
    uid, gid = parse_workspace_owner(owner)
    if not hasattr(os, "chown"):
        raise CloneSanitizationError(
            "this platform cannot set the workcell's ownership on the clone, so "
            "the workspace would arrive unwritable"
        )
    os.chown(root, uid, gid)
    for path in root.rglob("*"):
        # `follow_symlinks=False`: a symlink in the tree must not become a way
        # to change the ownership of whatever it points at outside the clone.
        os.chown(path, uid, gid, follow_symlinks=False)


def create_sanitized_clone(
    *,
    source_repository: Path,
    clone_path: Path,
    task_artifact_source: Path,
    task_artifact_destination: Path,
    workspace_owner: str | None = None,
    git_runner: GitRunner | None = None,
) -> SanitizedCloneReport:
    """Produce a disposable clone that the containment probes will accept.

    Fails closed: if the post-build audit finds anything, the clone directory is
    removed and `CloneSanitizationError` is raised. Returning a half-sanitised
    clone with a warning attached would guarantee that some future run mounts it.
    """

    run = git_runner or _default_git_runner
    source = source_repository.resolve()
    destination = clone_path.resolve()
    artifact_destination = task_artifact_destination.resolve()

    if not (source / ".git").exists():
        raise CloneSanitizationError(f"{source} is not a Git repository")
    if destination.exists():
        raise CloneSanitizationError(
            f"{destination} already exists; the clone must be disposable and "
            "built fresh, never reused across runs"
        )
    if not task_artifact_source.is_file():
        raise CloneSanitizationError(
            f"the task artifact {task_artifact_source} does not exist"
        )
    if audit_clone(
        remotes=(),
        entries=(),
        git_config_keys=(),
        task_artifact_path=str(artifact_destination),
        clone_path=str(destination),
    ):
        raise CloneSanitizationError(
            "the task artifact destination is inside the clone; it must be "
            "mounted read-only from outside the project tree"
        )

    head = run(["rev-parse", "HEAD"], source)
    if head.returncode != 0:
        raise CloneSanitizationError(
            f"could not read HEAD of {source}: {head.stderr.strip()}"
        )
    seed_commit = head.stdout.strip()

    destination.parent.mkdir(parents=True, exist_ok=True)
    # `--depth 1` needs a URL, not a path, or Git silently produces a full
    # clone -- which would carry the owner's entire history into a disposable
    # environment nobody reviewed for secrets.
    source_url = source.as_uri()
    cloned = run(
        [
            "clone",
            "--depth",
            "1",
            "--no-tags",
            "--no-hardlinks",
            "--quiet",
            source_url,
            str(destination),
        ],
        destination.parent,
    )
    if cloned.returncode != 0:
        raise CloneSanitizationError(
            f"could not clone {source}: {cloned.stderr.strip()}"
        )

    removed_remotes: list[str] = []
    listed = run(["remote"], destination)
    for remote in sorted(name for name in listed.stdout.split() if name):
        run(["remote", "remove", remote], destination)
        removed_remotes.append(remote)

    removed_config: list[str] = []
    listed_config = run(["config", "--local", "--list", "--name-only"], destination)
    for key in plan_git_config_removals(
        line.strip() for line in listed_config.stdout.splitlines() if line.strip()
    ):
        run(["config", "--local", "--unset-all", key], destination)
        removed_config.append(key)
    # A workcell commit must not be attributable to the owner.
    run(["config", "--local", "user.name", "apoapsis-workcell"], destination)
    run(["config", "--local", "user.email", "workcell@apoapsis.invalid"], destination)

    removed_paths = plan_removals(
        entry.name for entry in destination.iterdir() if entry.name != ".git"
    )
    for name in removed_paths:
        target = destination / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    if removed_paths:
        # Committed rather than left dirty: an unexplained dirty worktree at the
        # seed would be indistinguishable from the agent's own first edit when
        # the delta is computed.
        run(["add", "--all"], destination)
        run(
            [
                "commit",
                "--quiet",
                "--no-verify",
                "-m",
                "Sanitize disposable workcell clone",
            ],
            destination,
        )

    clone_head = run(["rev-parse", "HEAD"], destination)
    if clone_head.returncode != 0:
        raise CloneSanitizationError(
            f"could not read HEAD of the clone: {clone_head.stderr.strip()}"
        )

    shallow = (destination / ".git" / "shallow").exists()

    # The clone is built by the controller and consumed by the unprivileged
    # workcell user, and those are not the same identity. Without this the tree
    # arrives read-only and the `workspace-writable` containment probe reports a
    # breach -- correctly, because an agent that cannot edit its own worktree has
    # no baseline capability and every quality comparison downstream would be
    # measuring the mount, not the model.
    if workspace_owner is not None:
        _chown_tree(destination, workspace_owner)

    artifact_destination.parent.mkdir(parents=True, exist_ok=True)
    artifact_bytes = task_artifact_source.read_bytes()
    artifact_destination.write_bytes(artifact_bytes)

    audit_remotes = [
        name for name in run(["remote"], destination).stdout.split() if name
    ]
    audit_config = [
        line.strip()
        for line in run(
            ["config", "--local", "--list", "--name-only"], destination
        ).stdout.splitlines()
        if line.strip()
    ]
    findings = audit_clone(
        remotes=audit_remotes,
        entries=[entry.name for entry in destination.iterdir() if entry.name != ".git"],
        git_config_keys=audit_config,
        task_artifact_path=str(artifact_destination),
        clone_path=str(destination),
    )
    if not shallow:
        findings.append(
            "the clone is not shallow, so it carries owner history that was "
            "never reviewed for secrets into a disposable environment"
        )

    report = SanitizedCloneReport(
        source_repository=str(source),
        clone_path=str(destination),
        seed_commit=seed_commit,
        clone_head_commit=clone_head.stdout.strip(),
        removed_remotes=removed_remotes,
        removed_paths=removed_paths,
        removed_git_config_keys=removed_config,
        shallow=shallow,
        task_artifact_path=str(artifact_destination),
        task_artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        residual_findings=findings,
        sanitized=not findings,
    )
    if findings:
        shutil.rmtree(destination, ignore_errors=True)
        raise CloneSanitizationError(
            "the clone could not be sanitized and was destroyed rather than "
            "mounted: " + "; ".join(findings)
        )
    return report

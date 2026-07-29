"""Deterministic containment helpers for the Local Power Sandbox (ADR 0059).

Everything in this module is *harness* code. It exists so that a local model
running under `LocalPowerSession` can be given a looser action protocol --
whole-file writes and mediated shell -- without any widening of what it can
actually reach. Two boundaries are enforced here and nowhere else:

* `SandboxGuard` resolves every model-supplied path to a real location that
  provably lives inside one disposable sandbox root, and refuses the
  forbidden globs (Apoapsis internals, Git metadata, credential material)
  regardless of how the path was spelled.
* `SandboxShell` decides whether a model-supplied command string may run at
  all, and if so runs it with the sandbox as cwd, a scrubbed environment, a
  hard timeout, and capped captured output.

Neither helper ever consults the model's own claims, and neither can mark a
task complete. They only ever answer "is this specific request permitted".
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence


class SandboxViolation(RuntimeError):
    """A model-requested file or shell action was refused by the boundary.

    Raised for every rejection -- containment escapes, forbidden paths,
    disallowed commands, and exhausted budgets alike -- so `LocalPowerSession`
    can record a single, uniformly audited "rejected unsafe request" entry
    without having to distinguish harmless mistakes from probing attempts.
    """


# Shell metacharacters that would let one approved command string reach a
# second, unapproved program or an out-of-sandbox path through the shell
# rather than through argv. The sandbox never runs commands through a shell
# (`shell=False` below), but a command containing these is rejected outright
# instead of being silently reinterpreted as literal argv text.
_SHELL_METACHARACTERS = frozenset(";|&<>$`\n\r\\\"'*?!{}()[]")

# Environment variables that are never forwarded into a sandbox command even
# if they survive the caller's allowlist. Matched case-insensitively as
# substrings, so `MY_SERVICE_API_TOKEN` is caught as readily as `TOKEN`.
_SECRET_ENVIRONMENT_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "SESSION",
    "COOKIE",
)

# Command prefixes the sandbox will run when `allow_shell` is true. Each entry
# is matched against the *leading* argv tokens, so `python -m unittest discover
# -s tests -v` matches ("python", "-m", "unittest") and inherits its policy
# while `python -c "import os; os.system(...)"` matches nothing and is refused.
# Deliberately narrow: this is an allowlist, so anything not named here --
# `git`, `curl`, `powershell`, `cmd`, `rm`, `ssh` -- is denied by construction
# rather than by an easily-outdated deny list.
DEFAULT_ALLOWED_COMMAND_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "unittest"),
    ("python", "-m", "pytest"),
    ("python", "-m", "compileall"),
    ("python3", "-m", "unittest"),
    ("python3", "-m", "pytest"),
    ("python3", "-m", "compileall"),
    ("pytest",),
    ("node", "--test"),
    ("npm", "test"),
    ("npm", "run"),
)

# Prefixes that additionally require `allow_network = true`, because they
# resolve and download dependencies. With the default network-denied policy
# these are refused with an explanatory reason rather than silently failing
# partway through an install.
NETWORK_COMMAND_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("pip", "install"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("npm", "install"),
    ("npm", "ci"),
)


@dataclass(frozen=True)
class ShellPolicy:
    """The immutable decision inputs for whether one command may run."""

    allow_shell: bool
    allow_network: bool
    timeout_seconds: float
    max_output_chars: int
    allowed_prefixes: tuple[tuple[str, ...], ...] = DEFAULT_ALLOWED_COMMAND_PREFIXES
    network_prefixes: tuple[tuple[str, ...], ...] = NETWORK_COMMAND_PREFIXES
    environment_allowlist: tuple[str, ...] = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "TEMP",
        "TMP",
    )


@dataclass
class ShellOutcome:
    """A fully audited record of one mediated command execution."""

    command: str
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    cwd: str

    def summary(self) -> str:
        if self.timed_out:
            return f"{self.command!r} timed out"
        return f"{self.command!r} exited {self.exit_code}"


class SandboxGuard:
    """Resolve and authorize every model-supplied path against one root.

    The root is the disposable worktree; it is resolved once at construction
    (following any symlinks in the root itself) so that later comparisons are
    between two fully real paths. A request is permitted only when the real
    location of the deepest *existing* ancestor of the target still lies
    inside that real root -- which is what closes the symlink-escape hole that
    a purely lexical `..` check leaves open.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        forbidden_paths: Sequence[str],
        max_file_chars: int = 400_000,
        allow_binary: bool = False,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise SandboxViolation(f"sandbox root does not exist: {self.root}")
        self.forbidden_paths = tuple(forbidden_paths)
        self.max_file_chars = max_file_chars
        self.allow_binary = allow_binary

    # -- path authorization -------------------------------------------------

    def relative(self, raw: str) -> PurePosixPath:
        """Normalize one model-supplied path to a safe repository-relative form.

        Rejects absolute paths, Windows drive/UNC forms, home expansion, and
        any `..` traversal *before* touching the filesystem, so a hostile path
        never becomes a real filesystem operation even momentarily.
        """

        candidate = (raw or "").strip().replace("\\", "/")
        if not candidate:
            raise SandboxViolation("path must not be empty")
        if candidate.startswith("~"):
            raise SandboxViolation(
                f"path must not reference a home directory: {raw!r}"
            )
        if candidate.startswith("/") or candidate.startswith("//"):
            raise SandboxViolation(f"path must be sandbox-relative: {raw!r}")
        if len(candidate) >= 2 and candidate[1] == ":":
            raise SandboxViolation(
                f"path must not name a drive or device: {raw!r}"
            )
        if "\x00" in candidate:
            raise SandboxViolation("path must not contain a NUL byte")
        relative = PurePosixPath(candidate)
        parts = [part for part in relative.parts if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise SandboxViolation(
                f"path must not traverse outside the sandbox: {raw!r}"
            )
        if not parts:
            raise SandboxViolation("path must name a file, not the sandbox root")
        return PurePosixPath(*parts)

    def forbidden_reason(self, relative: PurePosixPath) -> str | None:
        """Return why ``relative`` is off-limits, or ``None`` if it is allowed.

        Every ancestor is checked too, so a `.git/**` rule blocks `.git`
        itself and `.git/hooks/pre-commit` alike, and a rule written against a
        directory can never be sidestepped by naming a file beneath it.
        """

        chain = [relative, *relative.parents]
        for pattern in self.forbidden_paths:
            normalized = pattern.replace("\\", "/")
            for item in chain:
                text = item.as_posix()
                if text in {"", "."}:
                    continue
                if fnmatch.fnmatch(text, normalized):
                    return (
                        f"path {relative.as_posix()!r} matches the forbidden "
                        f"pattern {pattern!r}"
                    )
                # `.git/**` should also match the bare directory `.git`.
                if normalized.endswith("/**") and text == normalized[:-3]:
                    return (
                        f"path {relative.as_posix()!r} matches the forbidden "
                        f"pattern {pattern!r}"
                    )
        return None

    def resolve(self, raw: str) -> Path:
        """Authorize ``raw`` and return the absolute path it may act on."""

        relative = self.relative(raw)
        reason = self.forbidden_reason(relative)
        if reason is not None:
            raise SandboxViolation(reason)
        target = self.root / Path(*relative.parts)
        self._assert_contained(target)
        return target

    def _assert_contained(self, target: Path) -> None:
        """Refuse a target whose real location escapes the sandbox root.

        Walks up to the deepest ancestor that actually exists and resolves
        *that*, which catches both a symlinked leaf and a symlinked parent
        directory pointing outside the sandbox. `Path.resolve()` on a
        non-existent path is non-committal about the missing tail, so the
        existing-ancestor form is the one that gives a real answer.
        """

        probe = target
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
        real = Path(os.path.realpath(probe))
        try:
            real.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation(
                f"resolved path escapes the sandbox root: {target}"
            ) from exc
        if target.is_symlink():
            raise SandboxViolation(f"path is a symlink: {target}")

    # -- content authorization ---------------------------------------------

    def read_text(self, raw: str) -> str:
        path = self.resolve(raw)
        if not path.is_file():
            raise SandboxViolation(f"file does not exist in the sandbox: {raw}")
        data = path.read_bytes()
        if len(data) > self.max_file_chars:
            data = data[: self.max_file_chars]
        if b"\x00" in data and not self.allow_binary:
            raise SandboxViolation(f"refusing to read a binary file: {raw}")
        return data.decode("utf-8", errors="replace")

    def validate_content(self, raw: str, content: str) -> None:
        if "\x00" in content and not self.allow_binary:
            raise SandboxViolation(f"refusing to write binary content to {raw}")
        if len(content) > self.max_file_chars:
            raise SandboxViolation(
                f"content for {raw} exceeds the {self.max_file_chars}-character "
                "per-file sandbox limit"
            )


@dataclass
class ChangeBudget:
    """Deterministic ceilings on how much one sandbox session may change."""

    max_changed_files: int
    max_changed_lines: int
    max_shell_commands: int
    max_seconds: float
    shell_commands_used: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)

    def remaining_seconds(self, *, now: float | None = None) -> float:
        elapsed = (now if now is not None else time.monotonic()) - self.started_monotonic
        return max(0.0, self.max_seconds - elapsed)

    def wall_clock_exhausted(self, *, now: float | None = None) -> bool:
        return self.remaining_seconds(now=now) <= 0.0

    def consume_shell_command(self) -> None:
        if self.shell_commands_used >= self.max_shell_commands:
            raise SandboxViolation(
                "sandbox shell-command budget is exhausted "
                f"({self.max_shell_commands} commands)"
            )
        self.shell_commands_used += 1

    def assert_change_size(self, *, changed_files: int, changed_lines: int) -> None:
        if changed_files > self.max_changed_files:
            raise SandboxViolation(
                f"change touches {changed_files} files, above the sandbox "
                f"ceiling of {self.max_changed_files}"
            )
        if changed_lines > self.max_changed_lines:
            raise SandboxViolation(
                f"change touches {changed_lines} diff lines, above the sandbox "
                f"ceiling of {self.max_changed_lines}"
            )


CommandRunner = Callable[..., subprocess.CompletedProcess]


class SandboxShell:
    """Run allowlisted commands inside the sandbox and nowhere else."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: ShellPolicy,
        guard: SandboxGuard,
        runner: CommandRunner | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self.guard = guard
        # Injectable purely so the deterministic tests can assert on cwd,
        # environment, and timeout without executing a real subprocess.
        self.runner = runner or subprocess.run
        self.environ = dict(os.environ if environ is None else environ)

    def parse(self, command: str) -> list[str]:
        """Turn a model-supplied command string into authorized argv."""

        if not self.policy.allow_shell:
            raise SandboxViolation(
                "shell execution is disabled for this sandbox session"
            )
        text = (command or "").strip()
        if not text:
            raise SandboxViolation("shell command must not be empty")
        illegal = sorted(_SHELL_METACHARACTERS.intersection(text))
        if illegal:
            raise SandboxViolation(
                "shell command must be a single plain program invocation; "
                f"refused metacharacters {illegal}"
            )
        try:
            argv = shlex.split(text, posix=True)
        except ValueError as exc:
            raise SandboxViolation(f"shell command could not be parsed: {exc}") from exc
        if not argv:
            raise SandboxViolation("shell command must not be empty")
        self._assert_allowed_prefix(argv)
        self._assert_path_arguments_contained(argv)
        return argv

    def _assert_allowed_prefix(self, argv: Sequence[str]) -> None:
        if _matches_any(argv, self.policy.network_prefixes):
            if not self.policy.allow_network:
                raise SandboxViolation(
                    f"command {argv[0]!r} requires network access, which is "
                    "disabled for this sandbox session"
                )
            return
        if _matches_any(argv, self.policy.allowed_prefixes):
            return
        allowed = sorted(" ".join(item) for item in self.policy.allowed_prefixes)
        raise SandboxViolation(
            f"command is not on the sandbox allowlist: {' '.join(argv)!r}; "
            f"allowed prefixes are {allowed}"
        )

    def _assert_path_arguments_contained(self, argv: Sequence[str]) -> None:
        """Any argument that looks like a path must stay inside the sandbox.

        Flags (`-v`, `--maxfail=1`) and bare module/test identifiers are left
        alone; anything containing a separator is run through the same
        `SandboxGuard` used for file actions, so `-s ../../etc` is refused
        by exactly the containment rule that refuses `read_file ../../etc`.
        """

        for token in argv[1:]:
            if token.startswith("-"):
                continue
            if "/" not in token and "\\" not in token:
                continue
            self.guard.relative(token)
            reason = self.guard.forbidden_reason(self.guard.relative(token))
            if reason is not None:
                raise SandboxViolation(f"command argument refused: {reason}")

    def environment(self) -> dict[str, str]:
        """Build the scrubbed environment one sandbox command may see."""

        selected: dict[str, str] = {}
        for name in self.policy.environment_allowlist:
            value = self.environ.get(name)
            if value is None:
                continue
            if _looks_secret(name):
                continue
            selected[name] = value
        # Marks the process as sandboxed for anything that wants to branch on
        # it, and keeps Python from writing caches back into the sandbox tree.
        selected["APOAPSIS_LOCAL_POWER_SANDBOX"] = "1"
        selected["PYTHONDONTWRITEBYTECODE"] = "1"
        if not self.policy.allow_network:
            # Belt-and-braces only: the allowlist already excludes network
            # tooling. These make an accidental outbound call from an allowed
            # test process fail fast and visibly rather than quietly succeed.
            selected["NO_PROXY"] = "*"
            selected["PIP_NO_INDEX"] = "1"
        return selected

    def run(self, command: str, *, timeout_seconds: float | None = None) -> ShellOutcome:
        argv = self.parse(command)
        timeout = min(
            self.policy.timeout_seconds,
            timeout_seconds if timeout_seconds is not None else self.policy.timeout_seconds,
        )
        started = time.monotonic()
        try:
            completed = self.runner(
                argv,
                cwd=str(self.root),
                env=self.environment(),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ShellOutcome(
                command=command,
                argv=list(argv),
                exit_code=None,
                stdout=_cap(_as_text(exc.stdout), self.policy.max_output_chars),
                stderr=_cap(_as_text(exc.stderr), self.policy.max_output_chars),
                timed_out=True,
                duration_seconds=time.monotonic() - started,
                cwd=str(self.root),
            )
        except OSError as exc:
            raise SandboxViolation(f"command could not start: {exc}") from exc
        return ShellOutcome(
            command=command,
            argv=list(argv),
            exit_code=completed.returncode,
            stdout=_cap(_as_text(completed.stdout), self.policy.max_output_chars),
            stderr=_cap(_as_text(completed.stderr), self.policy.max_output_chars),
            timed_out=False,
            duration_seconds=time.monotonic() - started,
            cwd=str(self.root),
        )


def _matches_any(
    argv: Sequence[str], prefixes: Iterable[tuple[str, ...]]
) -> bool:
    normalized = [_normalize_program(argv[0]), *argv[1:]]
    for prefix in prefixes:
        if len(normalized) < len(prefix):
            continue
        if all(normalized[index] == part for index, part in enumerate(prefix)):
            return True
    return False


def _normalize_program(program: str) -> str:
    """Compare `python.exe`, `./python`, and `python` as the same program."""

    name = PurePosixPath(program.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_ENVIRONMENT_MARKERS)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated at {limit} characters]"

"""The stable task kernel and the state capsule that survives compaction.

Two problems from the Crisis Atlas record meet here.

**The prompt supplied volume instead of navigation.** Slice prompts front-loaded
large excerpts, inherited tests, contracts, and replayed history. The
unrestricted control chose what to inspect as it worked and did better with
less. So the kernel is deliberately *small and stable*: objective, active
slice, obligations, contracts, forbidden operations, canonical commands, and
what a checkpoint means. Repository files stay on disk and are retrieved just
in time.

**Replay cost eight times the input tokens.** The control burned 2,080,801
input tokens against the sliced arm's 258,632, because the growing conversation
and every shell observation were resent on every call. A capsule fixes that:
after compaction, what survives is a structured summary of facts, not the
transcript they came from.

The other thing this module does is *layout*. Prompt-prefix caching only helps
when the prefix is byte-identical between calls, so the assembly order is
fixed — system prompt, deterministically sorted tool schemas, task kernel,
compacted history, latest observation — and nothing volatile is allowed into
the stable part. A timestamp in the kernel would silently cost every cache hit
in the session, and would look like nothing at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel

KERNEL_SCHEMA_VERSION = "1.0"
CAPSULE_SCHEMA_VERSION = "1.0"

#: Shapes that are *usually* volatile. These drive a diagnostic hint, never a
#: refusal. The first version of this module rejected them at construction,
#: which was the wrong test: a legitimate objective may quote a fixed upstream
#: UUID or a historical incident timestamp, and neither changes between calls.
#:
#: Volatility is a **provenance property, not a lexical one**. The thing that
#: matters is whether the kernel's bytes are the same bytes on every call, and
#: that is settled by `KernelArtifact`, not by a regex.
_VOLATILE_HINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timestamp", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("uuid", re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")),
    ("request id", re.compile(r"\bMRQ-[A-Za-z0-9._-]+")),
    ("elapsed time", re.compile(r"\b\d+(\.\d+)?\s?(ms|seconds?|minutes?) elapsed\b")),
)


class VolatilityHint(StrictModel):
    """A value in the kernel that *looks* volatile. Advisory only."""

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)
    note: str = Field(min_length=1)


def scan_volatility(text: str) -> list[VolatilityHint]:
    """Report volatile-looking values without judging them.

    Reported so an owner reviewing a kernel can ask "is that fixed?", and *not*
    enforced, because the enforcement that matters is byte reuse. A fixed
    upstream UUID in the objective is fine; a fresh one per run is not; the two
    are indistinguishable from the text of a single kernel.
    """

    hints: list[VolatilityHint] = []
    for label, pattern in _VOLATILE_HINT_PATTERNS:
        for match in pattern.finditer(text):
            hints.append(
                VolatilityHint(
                    label=label,
                    value=match.group(0),
                    note=(
                        f"this looks like a {label}. If it is fixed for the run "
                        "it is harmless; if it is regenerated per call it will "
                        "silently cost every prefix-cache hit in the session."
                    ),
                )
            )
    return hints


class TaskKernel(StrictModel):
    """The compact, read-only task document mounted for the agent.

    Every field is something the owner approved before the run. Nothing here is
    derived from the model's own output, and nothing changes turn to turn —
    which is what makes it cacheable and what makes its digest meaningful.
    """

    schema_version: str = KERNEL_SCHEMA_VERSION
    objective: str = Field(min_length=1)
    slice_id: str = Field(min_length=1)
    #: The obligations from the compiled `SliceAcceptanceContract`, rendered
    #: as text. The agent should be able to read what "done" means.
    acceptance_obligations: list[str] = Field(default_factory=list)
    architecture_contracts: list[str] = Field(default_factory=list)
    integration_contracts: list[str] = Field(default_factory=list)
    #: Operations the workcell will refuse. Stated so the agent does not spend
    #: turns discovering them.
    forbidden_operations: list[str] = Field(default_factory=list)
    #: Commands the agent may run to check its own work. Naming them is not
    #: permission to treat their passing as completion.
    canonical_commands: list[str] = Field(default_factory=list)
    checkpoint_instructions: str = Field(min_length=1)

    def volatility_hints(self) -> list[VolatilityHint]:
        """Volatile-looking values, for an owner to confirm are fixed.

        Deliberately not a validator. See `scan_volatility`.
        """

        return scan_volatility(self.render())

    def render(self) -> str:
        """Deterministic text. Same kernel, same bytes, every time."""

        def section(title: str, items: list[str]) -> list[str]:
            if not items:
                return []
            # Sorted, because a reordered list is a different prefix and the
            # order carries no meaning the agent needs.
            return [f"## {title}", *(f"- {item}" for item in sorted(items)), ""]

        lines = [
            "# Task",
            "",
            self.objective,
            "",
            f"## Active slice",
            f"- {self.slice_id}",
            "",
        ]
        lines += section("Acceptance obligations", self.acceptance_obligations)
        lines += section("Architecture contracts", self.architecture_contracts)
        lines += section("Integration contracts", self.integration_contracts)
        lines += section("Forbidden operations", self.forbidden_operations)
        lines += section("Commands available for self-testing", self.canonical_commands)
        lines += [
            "## Checkpoint",
            "",
            self.checkpoint_instructions,
            "",
            "Requesting a checkpoint asks to be inspected. It does not mark the",
            "slice complete: Apoapsis decides that from current-state evidence,",
            "and may return the slice to you with what is still outstanding.",
            "",
        ]
        return "\n".join(lines)

    def digest(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()


class KernelDriftError(RuntimeError):
    """The kernel artifact on disk is not the one the session was built with."""


class KernelArtifact(StrictModel):
    """The kernel as bytes on disk, built once and reused for every call.

    This is the real stability control. The kernel is rendered a single time at
    session start, written, hashed, and thereafter *read back* rather than
    re-rendered -- so a field that would have changed between calls cannot,
    because nothing re-renders it. A regex could only guess at which values
    were volatile; this makes the question moot.

    It also gives drift a name. If the file is edited mid-session, `load_text`
    raises rather than quietly serving different bytes under the same digest.
    """

    schema_version: str = KERNEL_SCHEMA_VERSION
    slice_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(gt=0)
    #: Volatile-looking values found at build time, recorded so the owner can
    #: confirm they are fixed. Their presence does not block anything.
    volatility_hints: list[VolatilityHint] = Field(default_factory=list)

    def load_text(self) -> str:
        """Read the exact bytes written at build time, or refuse."""

        raw = Path(self.path).read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != self.digest:
            raise KernelDriftError(
                f"the kernel artifact at {self.path} now hashes to {actual[:12]} "
                f"but the session was built on {self.digest[:12]}. Every call "
                "since the edit paid full prompt evaluation, and the prompts "
                "the model saw are no longer the ones recorded."
            )
        return raw.decode("utf-8")


def persist_kernel(kernel: TaskKernel, directory: str | Path) -> KernelArtifact:
    """Render the kernel once and freeze it.

    Called at session start, before the first model call. Everything after this
    point reads the file.
    """

    text = kernel.render()
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    destination = target / f"kernel-{kernel.slice_id}.md"
    raw = text.encode("utf-8")
    destination.write_bytes(raw)
    return KernelArtifact(
        slice_id=kernel.slice_id,
        path=str(destination),
        digest=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        volatility_hints=scan_volatility(text),
    )


class ObservedWitness(StrictModel):
    """One thing already proved, kept so it is not re-proved after compaction."""

    command_name: str = Field(min_length=1)
    passed: bool
    #: The worktree it observed. A capsule entry from an older fingerprint is
    #: history, not current evidence, and is labelled as such when rendered.
    worktree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = ""


class StateCapsule(StrictModel):
    """What survives compaction or a context rollover.

    The handoff's list, verbatim in intent: objective and slice obligations,
    the architecture/interface ledger, the changed-path and delta summary, the
    worktree fingerprint, witnesses already observed, latest failures,
    unresolved obligations, refused or no-progress actions, and the model's own
    concise notes — clearly marked advisory, because they are the one part of
    this document the model wrote.

    Raw terminal logs are deliberately absent. Once their important facts are
    represented here, replaying them is the eight-times-input-tokens mistake.
    """

    schema_version: str = CAPSULE_SCHEMA_VERSION
    slice_id: str = Field(min_length=1)
    worktree_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    unresolved_obligations: list[str] = Field(default_factory=list)
    interface_ledger: list[str] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    delta_summary: str = ""
    observed_witnesses: list[ObservedWitness] = Field(default_factory=list)
    latest_failures: list[str] = Field(default_factory=list)
    #: Actions the harness refused, and actions that produced no change. Kept
    #: so a fresh context does not immediately retry what already failed --
    #: the loop-detection halt in the Slice 2C sandbox arm was nine identical
    #: calls in a row.
    refused_actions: list[str] = Field(default_factory=list)
    no_progress_actions: list[str] = Field(default_factory=list)
    #: The model's own notes. Advisory: they are its beliefs about its work,
    #: not observations, and the rendering says so.
    model_notes: list[str] = Field(default_factory=list)
    #: Artifacts spilled from truncated tool output, retrievable on demand.
    artifact_pointers: list[str] = Field(default_factory=list)

    def render(self) -> str:
        """Text for the prompt. Compact, and honest about what is advisory."""

        lines = ["# State", "", f"Active slice: {self.slice_id}"]
        if self.worktree_fingerprint:
            lines.append(f"Worktree: {self.worktree_fingerprint[:12]}")
        lines.append("")

        def section(title: str, items: list[str], note: str = "") -> None:
            if not items:
                return
            lines.append(f"## {title}")
            if note:
                lines.append(note)
            lines.extend(f"- {item}" for item in items)
            lines.append("")

        section("Still outstanding", self.unresolved_obligations)
        section("Interfaces established", self.interface_ledger)
        section("Changed paths", self.changed_paths)
        if self.delta_summary:
            lines += ["## Delta", self.delta_summary, ""]
        if self.observed_witnesses:
            lines.append("## Evidence already observed")
            for item in self.observed_witnesses:
                mark = "passed" if item.passed else "FAILED"
                stale = (
                    ""
                    if item.worktree_fingerprint == self.worktree_fingerprint
                    else " (observed on an earlier worktree; not current evidence)"
                )
                lines.append(f"- {item.command_name}: {mark}{stale}")
            lines.append("")
        section("Latest failures", self.latest_failures)
        section(
            "Already refused -- do not retry unchanged",
            self.refused_actions,
        )
        section(
            "Produced no change -- try something different",
            self.no_progress_actions,
        )
        section("Retrievable artifacts", self.artifact_pointers)
        section(
            "Your own notes",
            self.model_notes,
            "Advisory. These are your beliefs from earlier turns, not "
            "observations, and may be wrong.",
        )
        return "\n".join(lines)


class PromptSection(StrEnum):
    """The fixed assembly order. Everything before `OBSERVATION` is cacheable."""

    SYSTEM = "system"
    TOOL_SCHEMAS = "tool_schemas"
    TASK_KERNEL = "task_kernel"
    HISTORY = "history"
    OBSERVATION = "observation"


#: The sections that must be byte-identical between calls for the provider's
#: prefix cache to hit. `HISTORY` is excluded: it grows, and that is expected.
STABLE_PREFIX_SECTIONS: tuple[PromptSection, ...] = (
    PromptSection.SYSTEM,
    PromptSection.TOOL_SCHEMAS,
    PromptSection.TASK_KERNEL,
)


class PromptLayout(StrictModel):
    """One assembled request, with its cacheable prefix identified."""

    system_prompt: str = Field(min_length=1)
    #: Rendered tool schemas, already sorted by the caller. Reordering them
    #: breaks the prefix even when the set is unchanged.
    tool_schemas: str = Field(min_length=1)
    task_kernel: str = Field(min_length=1)
    history: str = ""
    observation: str = ""

    def stable_prefix(self) -> str:
        return "\n\n".join(
            [self.system_prompt, self.tool_schemas, self.task_kernel]
        )

    def prefix_digest(self) -> str:
        return hashlib.sha256(self.stable_prefix().encode("utf-8")).hexdigest()

    def render(self) -> str:
        parts = [self.stable_prefix()]
        if self.history:
            parts.append(self.history)
        if self.observation:
            parts.append(self.observation)
        return "\n\n".join(parts)


class PrefixDrift(StrictModel):
    """Whether the cacheable prefix stayed identical across a session."""

    stable: bool
    call_count: int = Field(ge=0)
    distinct_prefixes: int = Field(ge=0)
    first_change_at_call: int | None = None
    detail: str = Field(min_length=1)


def check_prefix_stability(digests: list[str]) -> PrefixDrift:
    """Detect a prefix that moved mid-session.

    Worth checking explicitly because the symptom is invisible: the run still
    works, every answer is still correct, and the only evidence is a cache-hit
    rate that quietly went to zero. Slice 0's efficiency gate would report the
    resulting token cost as a property of the harness rather than as a bug.
    """

    if not digests:
        return PrefixDrift(
            stable=False,
            call_count=0,
            distinct_prefixes=0,
            detail="no calls were recorded, so prefix stability is unmeasured",
        )
    distinct = len(set(digests))
    if distinct == 1:
        return PrefixDrift(
            stable=True,
            call_count=len(digests),
            distinct_prefixes=1,
            detail=(
                f"the cacheable prefix was identical across all {len(digests)} "
                "call(s)"
            ),
        )
    first_change = next(
        index for index, item in enumerate(digests, start=1) if item != digests[0]
    )
    return PrefixDrift(
        stable=False,
        call_count=len(digests),
        distinct_prefixes=distinct,
        first_change_at_call=first_change,
        detail=(
            f"the cacheable prefix changed at call {first_change} and took "
            f"{distinct} distinct values; every call after the first change "
            "paid full prompt evaluation"
        ),
    )


def build_layout(
    *,
    system_prompt: str,
    tool_schemas: list[str],
    kernel: TaskKernel | KernelArtifact,
    capsule: StateCapsule | None = None,
    recent_history: str = "",
    observation: str = "",
) -> PromptLayout:
    """Assemble a request in the fixed order.

    `tool_schemas` is sorted here rather than trusted to arrive sorted, because
    a provider or a plugin reordering them is exactly the sort of change that
    would go unnoticed.

    `kernel` should be a `KernelArtifact` in a live session: passing the model
    re-renders it, which is correct today and is the thing that would silently
    stop being correct if a field ever became dynamic.
    """

    history_parts = []
    if capsule is not None:
        history_parts.append(capsule.render())
    if recent_history:
        history_parts.append(recent_history)
    return PromptLayout(
        system_prompt=system_prompt,
        tool_schemas="\n".join(sorted(tool_schemas)),
        task_kernel=(
            kernel.load_text()
            if isinstance(kernel, KernelArtifact)
            else kernel.render()
        ),
        history="\n\n".join(history_parts),
        observation=observation,
    )

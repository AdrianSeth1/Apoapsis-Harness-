"""What prior slices built, stated deterministically so nobody has to explore.

Every slice runs in a fresh CLI session. That is the right call — no cross-slice
contamination, and inherited state travels through *code* rather than through a
context window nobody can audit. The cost it creates is re-orientation: slice N
opens with an agent that knows nothing about the N-1 slices already in the
repository, so it reads its way back to a working picture. CAP-4EE9F101146E4556
spent 44 of its 122 tool calls on `read_file`, rediscovering files earlier
slices had written and the harness already knew everything about.

That cost grows with the repository, which is what makes it a problem rather
than an annoyance: by slice 10 of a 40-slice plan, sessions cross the
compression threshold mid-work, and mid-slice compression is where quality
falls over.

So the harness states what it already knows, before the agent starts looking.
Nothing here is a model call, an inference, or a summary: the tree is walked,
the line counts are counted, the provenance is read out of checkpoint records
that already exist. Everything in this brief could be recomputed byte-for-byte
from the same inputs.

Two deliberate non-goals. It does **not** forbid exploration — a brief that
lied, or that quietly omitted the one file the slice needed, would be worse
than none, and the agent must always be able to check. And it is **bounded**:
past a few thousand tokens the brief becomes the context problem it exists to
solve, so it degrades to directories plus the files this slice is actually
about, rather than growing with the repository.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.delta import EXCLUDED_METADATA_NAMES

#: Excluded on top of the delta's metadata names. `.apoapsis` and `.sol` are
#: deliberately *not* excluded from the delta -- an agent writing into them is a
#: boundary violation admission must see and refuse -- but they are harness
#: state, not the product's code, and describing them to the agent as inherited
#: work would be false as well as enormous. Pointed at a real project they are
#: thousands of audit files.
_NOT_PRODUCT_STATE: frozenset[str] = frozenset({".apoapsis", ".sol"})

_TREE_EXCLUDED: frozenset[str] = EXCLUDED_METADATA_NAMES | _NOT_PRODUCT_STATE

#: Rough characters-per-token, matching the estimate used elsewhere.
CHARS_PER_TOKEN_ESTIMATE = 4

#: The brief's ceiling. ~2-3K tokens: enough for a small project's whole tree
#: and every prior slice's contribution, small beside a 65K window, and far
#: below what the 44 `read_file` calls it replaces cost to discover.
MAX_ORIENTATION_TOKENS = 2_500

#: ASCII only, deliberately. This text crosses Windows -> WSL -> container and
#: back, and a run has already been lost to a decode error on that path (see
#: `product.NativeQwenWorkcellExecutor.run`). Nothing here is worth a
#: non-ASCII character.
_HEADER = "Inherited state - read before exploring"


class SliceContribution(StrictModel):
    """What one completed prior slice put into the repository.

    Read from that slice's checkpoint record, which is evidence the harness
    produced itself: `paths` is what its admitted candidate changed, and
    `behaviour_names` are the additions its witnesses proved were reached.
    """

    slice_id: str = Field(min_length=1)
    title: str = ""
    paths: list[str] = Field(default_factory=list)
    #: Files, symbols and routes this slice introduced, by the names the
    #: checkpoint recorded. Useful precisely because they are the things a
    #: later slice is most likely to want to call.
    behaviour_names: list[str] = Field(default_factory=list)


def _counted_tree(root: Path) -> list[tuple[str, int]]:
    """Every ordinary file under `root`, with its line count.

    Metadata and caches are excluded by the same name set the delta uses, so
    the brief describes the same tree admission will judge. Binary and
    undecodable files are reported with a line count of zero rather than
    skipped: their existence is part of the picture even when their contents
    are not.
    """

    entries: list[tuple[str, int]] = []
    if not root.is_dir():
        return entries
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(
            item for item in directories if item not in _TREE_EXCLUDED
        )
        for name in sorted(names):
            if name in _TREE_EXCLUDED:
                continue
            absolute = Path(current) / name
            if absolute.is_symlink() or not absolute.is_file():
                continue
            relative = absolute.relative_to(root).as_posix()
            try:
                lines = absolute.read_text(encoding="utf-8").count("\n")
            except (OSError, UnicodeDecodeError):
                lines = 0
            entries.append((relative, lines))
    return sorted(entries)


def _relevance(path: str, focus: tuple[str, ...]) -> int:
    """Lower sorts earlier. Files this slice is about come first.

    Used only when the whole tree does not fit. The ordering is by declared
    relevance, not by guesswork about the code: a path the slice names, then a
    path sharing a directory with one, then everything else.
    """

    for item in focus:
        if path == item:
            return 0
    directories = {item.rsplit("/", 1)[0] for item in focus if "/" in item}
    if any(path.startswith(f"{item}/") for item in directories):
        return 1
    return 2


def _directory_summary(entries: list[tuple[str, int]], budget: int) -> list[str]:
    """One line per directory, for a tree too large to list file by file.

    Bounded like everything else here. A repository with thousands of
    directories would otherwise blow the cap in the very branch that exists to
    enforce it -- the summary has to be smaller than what it summarises, or it
    is not a summary. Largest directories first, since those are the ones worth
    knowing about, and the count of what was left out is stated rather than
    quietly dropped.
    """

    totals: dict[str, tuple[int, int]] = {}
    for path, lines in entries:
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        files, counted = totals.get(directory, (0, 0))
        totals[directory] = (files + 1, counted + lines)

    ranked = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
    chosen: list[str] = []
    remaining = budget
    for directory, (files, lines) in ranked:
        line = f"  {directory}/ - {files} file(s), {lines} lines"
        if len(line) + 1 > remaining:
            break
        chosen.append(line)
        remaining -= len(line) + 1
    omitted = len(ranked) - len(chosen)
    summary = sorted(chosen)
    if omitted:
        summary.append(f"  ... and {omitted} more director(ies) not listed here")
    return summary


def build_orientation_brief(
    base: Path,
    *,
    contributions: list[SliceContribution] | None = None,
    integration_contracts: list[str] | None = None,
    commands: list[str] | None = None,
    focus_paths: list[str] | None = None,
    max_tokens: int = MAX_ORIENTATION_TOKENS,
) -> str:
    """The brief for one slice, or `""` when there is nothing to say.

    An empty brief is returned for an empty base rather than a section
    announcing that the repository is empty: slice 1 of a new project has no
    inherited state, and telling it so at length is the tax this exists to cut.
    """

    entries = _counted_tree(base)
    contributions = contributions or []
    integration_contracts = integration_contracts or []
    commands = commands or []
    focus = tuple(focus_paths or [])

    if not entries and not contributions and not integration_contracts:
        return ""

    fixed: list[str] = [f"{_HEADER}\n"]
    fixed.append(
        "Everything below was produced by Apoapsis from the repository itself, "
        "not by a model. It is here so you do not have to rediscover it. Read "
        "files when you need their contents; you should not need to go looking "
        "for what exists.\n"
    )

    if contributions:
        fixed.append("Built by earlier slices of this plan:")
        for item in contributions:
            title = f" - {item.title}" if item.title else ""
            fixed.append(f"- {item.slice_id}{title}")
            if item.paths:
                fixed.append(f"    files: {', '.join(item.paths)}")
            if item.behaviour_names:
                fixed.append(f"    provides: {', '.join(item.behaviour_names)}")
        fixed.append("")

    if integration_contracts:
        fixed.append("Interfaces this slice is expected to meet:")
        fixed.extend(f"- {item}" for item in integration_contracts)
        fixed.append("")

    if commands:
        fixed.append("Checks Apoapsis will run, exactly as it runs them:")
        fixed.extend(f"- {item}" for item in commands)
        fixed.append("")

    budget = max_tokens * CHARS_PER_TOKEN_ESTIMATE
    spent = sum(len(line) + 1 for line in fixed)

    tree_lines = [f"  {path} - {lines} lines" for path, lines in entries]
    tree_cost = sum(len(line) + 1 for line in tree_lines) + len("Current files:\n")

    if not entries:
        body: list[str] = []
    elif spent + tree_cost <= budget:
        body = ["Current files:", *tree_lines]
    else:
        # Too large to list whole. Directories always, then as many of this
        # slice's own files as fit -- the ones it was told to work on are the
        # ones it would otherwise spend its first turns finding.
        body = ["Current files (summarised; the tree is larger than this brief):"]
        # Two thirds of what is left to the directory shape, one third held
        # back for this slice's own files: the shape is the more useful half,
        # but arriving with no named file at all would leave the agent
        # searching for the exact paths it was told to write.
        head = len(body[0]) + 1
        available = max(budget - spent - head, 0)
        body.extend(_directory_summary(entries, (available * 2) // 3))
        remaining = budget - spent - sum(len(line) + 1 for line in body)
        ranked = sorted(entries, key=lambda item: (_relevance(item[0], focus), item[0]))
        selected: list[str] = []
        for path, lines in ranked:
            if _relevance(path, focus) == 2:
                break
            line = f"  {path} - {lines} lines"
            if len(line) + 1 > remaining:
                break
            selected.append(line)
            remaining -= len(line) + 1
        if selected:
            body.append("Files relevant to this slice:")
            body.extend(sorted(selected))

    return "\n".join([*fixed, *body]).rstrip() + "\n\n"


__all__ = [
    "CHARS_PER_TOKEN_ESTIMATE",
    "MAX_ORIENTATION_TOKENS",
    "SliceContribution",
    "build_orientation_brief",
]

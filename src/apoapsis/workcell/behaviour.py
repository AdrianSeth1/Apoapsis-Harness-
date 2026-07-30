"""What behaviour this candidate actually added, at symbol and route level.

Slice 4's new-component rule only looked at *added files*. That was the right
first cut and it has a hole the Crisis Atlas record names precisely: Slice 3's
nonexistent static directory, unreachable export routes, and non-serializable
timeline responses all lived in a **modified** file. A file-level rule sees
`api/server.py` was already covered by the inherited suite and asks no further
question.

So the unit of the rule becomes a **behaviour unit**: a whole added production
file, a new top-level function or class inside a modified one, or a new route
literal. Each carries its line range, so coverage at line granularity can say
whether that specific addition was reached rather than whether its file was.

Extraction is deliberately conservative and syntactic:

* Python symbols come from `ast`, so they are exact for Python.
* Routes come from a narrow regex over *added* lines only. This is a heuristic
  and is labelled one — a route it misses is a gap, a route it invents is a
  false obligation the owner can see and delete. It never silently widens what
  counts as covered.

Nothing here reads the workcell's claims. Both sides of the comparison are
trees the controller materialised.
"""

from __future__ import annotations

import ast
import re
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel
from apoapsis.workcell.delta import CandidateDelta, ChangeKind, PathClass


class BehaviourKind(StrEnum):
    #: A whole production file that did not exist before.
    NEW_FILE = "new_file"
    #: A top-level function or class that did not exist in the base version.
    NEW_SYMBOL = "new_symbol"
    #: A route literal introduced by this candidate.
    NEW_ROUTE = "new_route"


class BehaviourUnit(StrictModel):
    """One addition that must be shown to be reachable."""

    kind: BehaviourKind
    path: str = Field(min_length=1)
    #: Symbol name, route string, or the path again for a whole new file.
    name: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    #: True when this was found by the route heuristic rather than by parsing.
    heuristic: bool = False

    @property
    def unit_id(self) -> str:
        return f"{self.path}::{self.name}"


#: Conservative: a quoted absolute path, at least one segment, no spaces. Kept
#: narrow on purpose -- a regex that matched every string would turn ordinary
#: constants into obligations nobody could discharge.
_ROUTE_PATTERN = re.compile(r"""["'](/[A-Za-z0-9_\-./{}<>:]*)["']""")
#: A route literal only counts when the line looks like routing, not like an
#: arbitrary string that happens to start with a slash.
_ROUTE_CONTEXT = re.compile(
    r"\b(route|get|post|put|patch|delete|add_url_rule|path|endpoint|urlpatterns)\b",
    re.IGNORECASE,
)


def _python_symbols(source: str) -> dict[str, tuple[int, int]]:
    """Top-level functions and classes, with their line ranges.

    Returns an empty mapping for anything that does not parse. A syntax error
    is a real condition -- the candidate does not compile -- and it is caught
    by the verification command rather than guessed at here.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    symbols: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols[node.name] = (node.lineno, getattr(node, "end_lineno", node.lineno))
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        symbols[f"{node.name}.{child.name}"] = (
                            child.lineno,
                            getattr(child, "end_lineno", child.lineno),
                        )
    return symbols


def _routes(source: str) -> dict[str, int]:
    """Route literals and the line they appear on, from routing-looking lines."""

    found: dict[str, int] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        if not _ROUTE_CONTEXT.search(line):
            continue
        for match in _ROUTE_PATTERN.finditer(line):
            found.setdefault(match.group(1), number)
    return found


def _read(root: Path, relative: str) -> str | None:
    path = Path(root) / relative
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def changed_behaviour(
    delta: CandidateDelta, base_root: str | Path, candidate_root: str | Path
) -> list[BehaviourUnit]:
    """Every behaviour unit this candidate added.

    An added production file yields one `NEW_FILE` unit covering the whole
    file, plus nothing else: requiring every symbol in a brand-new module to be
    individually covered would be stricter than the handoff asks and would make
    a helper function block a slice.

    A *modified* production file yields a unit for each top-level symbol and
    each route that is present in the candidate and absent from the base. This
    is the Slice 3 gap: the file was already covered, the addition may not be.
    """

    units: list[BehaviourUnit] = []
    for entry in delta.entries:
        if entry.path_class != PathClass.PRODUCTION or entry.binary:
            continue
        if entry.kind == ChangeKind.DELETED:
            continue

        candidate_source = _read(candidate_root, entry.path)
        if candidate_source is None:
            continue

        if entry.kind == ChangeKind.ADDED:
            line_count = max(1, len(candidate_source.splitlines()))
            units.append(
                BehaviourUnit(
                    kind=BehaviourKind.NEW_FILE,
                    path=entry.path,
                    name=entry.path,
                    start_line=1,
                    end_line=line_count,
                )
            )
            continue

        base_source = _read(base_root, entry.path) or ""
        if entry.path.endswith(".py"):
            before = _python_symbols(base_source)
            after = _python_symbols(candidate_source)
            for name, (start, end) in sorted(after.items()):
                if name not in before:
                    units.append(
                        BehaviourUnit(
                            kind=BehaviourKind.NEW_SYMBOL,
                            path=entry.path,
                            name=name,
                            start_line=start,
                            end_line=end,
                        )
                    )

        before_routes = _routes(base_source)
        for route, line in sorted(_routes(candidate_source).items()):
            if route not in before_routes:
                units.append(
                    BehaviourUnit(
                        kind=BehaviourKind.NEW_ROUTE,
                        path=entry.path,
                        name=route,
                        start_line=line,
                        end_line=line,
                        heuristic=True,
                    )
                )
    return units

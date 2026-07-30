from __future__ import annotations

import json
import re
from typing import Any


# A leading fence line: one or more backticks or tildes, optionally followed by
# a language tag (```json, `json, ~~~JSON). Nothing else is recognized.
_OPENING_FENCE = re.compile(r"^(?P<marks>`{1,}|~{3,})[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n")
_CLOSING_FENCE = re.compile(r"\r?\n(?P<marks>`{1,}|~{3,})[ \t]*$")


class PastedJsonError(ValueError):
    """A pasted response could not be read as JSON, with the offending text."""


def normalize_pasted_json_text(raw_text: str) -> tuple[str, list[str]]:
    """Strip the two wrappers a chat interface reliably adds, and nothing else.

    Returns the cleaned text and a list of human-readable notes describing what
    was removed, so the normalization is auditable rather than invisible.

    Deliberately narrow. A UTF-8 byte-order mark and a Markdown code fence are
    artifacts of *how the text was transported*, not content a model chose to
    write, and both put a non-JSON character at position 0 where they defeat
    parsing entirely. Anything beyond that -- prose preambles, trailing
    commentary, scanning forward for the first `{` -- is guesswork about
    meaning and is not attempted: those still fail, loudly, with the text
    shown.
    """

    notes: list[str] = []
    text = raw_text
    if text.startswith("﻿"):
        text = text[1:]
        notes.append("removed a UTF-8 byte-order mark")
    stripped = text.strip()
    opening = _OPENING_FENCE.match(stripped)
    if opening is not None:
        stripped = stripped[opening.end() :]
        closing = _CLOSING_FENCE.search(stripped)
        if closing is not None:
            stripped = stripped[: closing.start()]
        notes.append("removed a surrounding Markdown code fence")
    return stripped.strip(), notes


def parse_pasted_json(raw_text: str, *, what: str = "response") -> Any:
    """Parse operator-pasted JSON after bounded, auditable normalization.

    On failure the error quotes the start of what was actually received. The
    previous message -- `response is not valid JSON: Expecting value: line 1
    column 1 (char 0)` -- named the position of the problem without showing the
    character sitting there, which is the one thing that makes it obvious.
    """

    text, notes = normalize_pasted_json_text(raw_text)
    if not text:
        raise PastedJsonError(f"{what} is empty after removing formatting wrappers")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:80].replace("\n", "\\n")
        suffix = "..." if len(text) > 80 else ""
        detail = f"{what} is not valid JSON: {exc}"
        if notes:
            detail += f" (after {', '.join(notes)})"
        detail += f'; it starts: "{preview}{suffix}"'
        raise PastedJsonError(detail) from exc


__all__ = ["PastedJsonError", "normalize_pasted_json_text", "parse_pasted_json"]

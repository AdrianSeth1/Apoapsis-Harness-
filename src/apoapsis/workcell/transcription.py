"""Model transcription fidelity, measured and reported but never gating.

ADR 0078 moves `multiline_unicode_integrity` onto a deterministic echo provider
so that it measures the envelope instead of the model. The signal it stops
carrying is real and worth keeping: Slice 2B observed Qwen3.6-27B retyping
`U+2018`/`U+2019` as ASCII apostrophes while every other character -- an
astral-plane emoji with its variation selector, an em-dash, and two CJK
characters -- survived byte-exact. An agent that silently normalises quotes will
eventually normalise them inside a file it was asked to copy verbatim.

So the measurement stays, under its own name, with two properties the
conformance suite must not blur:

* **It is model behaviour, not adapter behaviour.** Every record says so in a
  field, not only in prose, because the 2B failure happened precisely by
  reading a model result off an adapter-shaped report.
* **It does not gate.** There is no `ConformanceStatus` here and nothing in
  this module reaches `evaluate_conformance` or `evaluate_slice3_gate`. A model
  that transcribes badly is a quality finding for the paired scorecard, not a
  reason to refuse to measure quality.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum

from pydantic import ConfigDict, Field

from apoapsis.specification.schema import StrictModel


class TranscriptionAttribution(StrEnum):
    """What a transcription result is evidence about.

    A single-member enum today, and deliberately an enum rather than a bare
    string: it exists so that a reader of the JSON, or a later function that
    aggregates these records, cannot mistake the subject of the measurement.
    """

    MODEL_BEHAVIOUR = "model_behaviour"


class CodepointDifference(StrictModel):
    """One substitution, named in a form a human can act on.

    `str_strip_whitespace` is switched off for the same reason it is in
    `echo_provider.EchoExchange`: several of the substitutions worth recording
    *are* whitespace, and a record that trimmed them would report that a space
    became an empty string.
    """

    model_config = ConfigDict(str_strip_whitespace=False)

    index: int = Field(ge=0)
    sent: str
    received: str
    sent_name: str = ""
    received_name: str = ""


class TranscriptionFidelity(StrictModel):
    """How faithfully the model retyped a string it was told to copy exactly."""

    schema_version: str = "1.0"
    #: Fixed by construction. See `TranscriptionAttribution`.
    attribution: TranscriptionAttribution = TranscriptionAttribution.MODEL_BEHAVIOUR
    gating: bool = False
    measured: bool = True
    exact: bool = False
    sent_chars: int = Field(default=0, ge=0)
    received_chars: int = Field(default=0, ge=0)
    #: Length-preserving substitutions, listed. Bounded so a model that returns
    #: unrelated prose cannot turn one metric into a megabyte of evidence.
    differences: list[CodepointDifference] = Field(default_factory=list)
    truncated_report: bool = False
    detail: str = ""


#: Enough to characterise a substitution habit, few enough to stay readable.
MAX_REPORTED_DIFFERENCES = 25


def measure_transcription_fidelity(
    *, sent: str, received: str | None
) -> TranscriptionFidelity:
    """Compare what the model was given against what it typed back.

    Never raises and never fails: an unmeasurable result is reported as
    `measured=False`, which is honest, and is safe here only because nothing
    downstream treats this record as a pass or a failure.
    """

    if received is None:
        return TranscriptionFidelity(
            measured=False,
            sent_chars=len(sent),
            detail=(
                "the model returned no transcribable content, so its "
                "transcription fidelity was not measured"
            ),
        )
    if sent == received:
        return TranscriptionFidelity(
            exact=True,
            sent_chars=len(sent),
            received_chars=len(received),
            detail=f"the model retyped all {len(sent)} characters exactly",
        )

    differences: list[CodepointDifference] = []
    for index, (want, got) in enumerate(zip(sent, received)):
        if want == got:
            continue
        if len(differences) >= MAX_REPORTED_DIFFERENCES:
            break
        differences.append(
            CodepointDifference(
                index=index,
                sent=want,
                received=got,
                sent_name=_codepoint_name(want),
                received_name=_codepoint_name(got),
            )
        )
    # Positional comparison stops being meaningful once the lengths diverge, so
    # the length change is reported as its own fact rather than as a cascade of
    # thousands of "differences" that are really one insertion.
    length_note = (
        ""
        if len(sent) == len(received)
        else (
            f"; the length also changed from {len(sent)} to {len(received)} "
            "characters, so positions after the first insertion or deletion "
            "are not aligned"
        )
    )
    return TranscriptionFidelity(
        exact=False,
        sent_chars=len(sent),
        received_chars=len(received),
        differences=differences,
        truncated_report=len(differences) >= MAX_REPORTED_DIFFERENCES,
        detail=(
            f"the model altered {len(differences)} of the first "
            f"{min(len(sent), len(received))} aligned character(s)"
            + length_note
            + ". This is model behaviour and does not gate conformance"
        ),
    )


def _codepoint_name(char: str) -> str:
    try:
        return unicodedata.name(char)
    except ValueError:
        return f"U+{ord(char):04X}"

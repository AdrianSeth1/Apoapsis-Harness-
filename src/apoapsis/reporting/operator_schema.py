"""The shape of an operator-facing stop explanation.

Separated from `reporting.operator`, which builds them: the builders import the
outcome enums from `workcell` and `review`, and those modules need to *carry* an
explanation on their own records. Keeping the model in a leaf module with no
project imports is what lets both sides use it without a cycle.
"""

from __future__ import annotations

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class OperatorExplanation(StrictModel):
    """One stop, in the three parts an operator can act on.

    The harness's own stop text is precise and, to an operator, unreadable:
    "no witness survived validation, so nothing current proves anything about
    this candidate" is exactly true and says neither what happened nor what to
    do. The internal vocabulary is not the problem -- it earns its keep inside
    the harness -- but it should not be the first thing a person reads.
    """

    #: What the operator asked for, in their words.
    attempted: str = Field(min_length=1)
    #: What refused it and why, in one sentence, naming the check rather than
    #: the class that implements it.
    refusal: str = Field(min_length=1)
    #: Exactly one recommended next action. Singular on purpose: a stop that
    #: offers three options offers none, because choosing between them needs
    #: the knowledge the operator does not have.
    next_action: str = Field(min_length=1)
    #: The harness's own words, verbatim, shown second. Nothing is hidden; the
    #: ordering is the whole change.
    detail: str = ""

    @property
    def summary(self) -> str:
        return f"{self.attempted} {self.refusal} {self.next_action}"


__all__ = ["OperatorExplanation"]

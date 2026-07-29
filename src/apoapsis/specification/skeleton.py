from __future__ import annotations

import enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel


_MAX_DEPTH = 12


def _placeholder_for_scalar(annotation: Any) -> Any:
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    return "<string>"


def _unwrap_optional(annotation: Any) -> Any:
    """Return the meaningful member of an `X | None` union.

    Optional fields still need a shape in the skeleton: the whole point is to
    show the reader every key that may legitimately appear, and a bare `null`
    teaches them nothing about the object they are supposed to produce.
    """

    origin = get_origin(annotation)
    if origin is not Union and str(origin) != "<class 'types.UnionType'>":
        return annotation
    candidates = [item for item in get_args(annotation) if item is not type(None)]
    return candidates[0] if candidates else annotation


def _skeleton_for_annotation(annotation: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return "<...>"
    annotation = _unwrap_optional(annotation)
    origin = get_origin(annotation)

    if origin is Literal:
        options = get_args(annotation)
        return options[0] if options else "<string>"
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        # Every permitted value, not just the first one (ADR 0075). Rendering
        # `members[0].value` produced a placeholder that looked like a real
        # answer, so a reader copying the shape kept whichever value happened
        # to sort first. For `IntegrationContract.runtime_boundary` that value
        # is `unspecified`, which is precisely the one that asserts nothing
        # and therefore disables ADR 0074's contradiction check.
        #
        # The `<one of: ...>` wrapper matches the existing `<string>`
        # convention: obviously a placeholder to be replaced, never mistaken
        # for content. Still derived from the model, so it cannot drift from
        # what validation accepts.
        members = list(annotation)
        if not members:
            return "<string>"
        return "<one of: " + "|".join(str(item.value) for item in members) + ">"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return json_skeleton(annotation, depth=depth + 1)
    if origin in {list, set, tuple}:
        args = get_args(annotation)
        if not args:
            return []
        return [_skeleton_for_annotation(args[0], depth + 1)]
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        return _placeholder_for_scalar(annotation)
    return "<string>"


def json_skeleton(model: type[BaseModel], *, depth: int = 0) -> dict[str, Any]:
    """A fully expanded example object showing every permitted key of `model`,
    including every nested model, with placeholder values.

    Pydantic's `model_json_schema()` is correct and complete, but it is written
    for a validator: nested models become `$ref` pointers into a `$defs`
    section, so a reader has to resolve indirection to learn what keys an
    object actually takes. ADR 0066: a frontier model handed the discovery
    planning package produced correct field names for every section the
    handoff also described in prose, and invented plausible-but-wrong keys for
    the two sections that existed only behind `$ref` -- `delivery_contract`
    and `verification_strategy` -- which then failed `extra_forbidden`
    validation after the whole plan had been written.

    This renders the same information as one literal shape that can be copied
    directly. It is derived from the models themselves, so it can never drift
    away from what validation actually enforces.
    """

    skeleton: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        skeleton[name] = _skeleton_for_annotation(field.annotation, depth)
    return skeleton


__all__ = ["json_skeleton"]

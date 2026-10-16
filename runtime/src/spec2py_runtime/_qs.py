"""Serialize path and query parameters following OpenAPI ``style``/``explode`` rules."""

import enum
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Literal, cast

import pydantic

from ._types import NotGiven

__all__ = ["QueryStyle", "encode_query", "path_template", "query_param"]

type QueryStyle = Literal["form", "spaceDelimited", "pipeDelimited", "deepObject"]
type QueryItems = list[tuple[str, str]]


def _scalar(value: object) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case None:
            return ""
        case enum.Enum():
            return _scalar(value.value)
        case datetime() | date() | time():
            return value.isoformat()
        case _:
            return str(value)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, pydantic.BaseModel):
        return cast(dict[str, object], value.model_dump(mode="json", by_alias=True))
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def query_param(
    name: str,
    value: object,
    *,
    style: QueryStyle = "form",
    explode: bool = True,
) -> QueryItems:
    """Encode one query parameter into ``(key, value)`` pairs."""
    if isinstance(value, NotGiven):
        return []
    if (mapping := _as_mapping(value)) is not None:
        if style == "deepObject":
            return [(f"{name}[{k}]", _scalar(v)) for k, v in mapping.items()]
        if explode:
            return [(k, _scalar(v)) for k, v in mapping.items()]
        flat = ",".join(f"{k},{_scalar(v)}" for k, v in mapping.items())
        return [(name, flat)]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        items = [_scalar(v) for v in cast(Sequence[object], value)]
        if explode and style == "form":
            return [(name, v) for v in items]
        sep = {"spaceDelimited": " ", "pipeDelimited": "|"}.get(style, ",")
        return [(name, sep.join(items))]
    return [(name, _scalar(value))]


def encode_query(
    params: Mapping[str, object],
    styles: Mapping[str, tuple[QueryStyle, bool]] | None = None,
) -> QueryItems:
    """Encode a mapping of query parameters, skipping ``NOT_GIVEN`` values.

    ``styles`` maps a parameter name to its ``(style, explode)`` when it isn't the
    OpenAPI default of ``("form", True)``.
    """
    items: QueryItems = []
    for name, value in params.items():
        style, explode = (styles or {}).get(name, ("form", True))
        items.extend(query_param(name, value, style=style, explode=explode))
    return items


def path_template(template: str, /, **values: object) -> str:
    """Fill ``{name}`` placeholders in a path, percent-encoding each value."""
    out = template
    for name, value in values.items():
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            text = ",".join(_scalar(v) for v in cast(Sequence[object], value))
        else:
            text = _scalar(value)
        out = out.replace("{" + name + "}", urllib.parse.quote(text, safe=""))
    return out

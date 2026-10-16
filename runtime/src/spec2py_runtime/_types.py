"""Shared types: the ``NOT_GIVEN`` sentinel, request option aliases and file types."""

import os
from collections.abc import Mapping
from typing import IO, Final, Literal, TypeIs, final, override

import httpx2

__all__ = [
    "NOT_GIVEN",
    "Body",
    "FileContent",
    "FileTypes",
    "Headers",
    "NotGiven",
    "Query",
    "Timeout",
    "TimeoutTypes",
    "is_given",
]


@final
class NotGiven:
    """Marks a parameter the caller did not pass.

    Distinct from ``None`` so that "omit this field" and "send null" stay different::

        client.invoices.update("inv_1", memo=None)  # sends {"memo": null}
        client.invoices.update("inv_1")  # sends {}
    """

    _instance: NotGiven | None = None

    def __new__(cls) -> NotGiven:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> Literal[False]:
        return False

    @override
    def __repr__(self) -> str:
        return "NOT_GIVEN"

    def __reduce__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN: Final = NotGiven()


def is_given[T](value: T | NotGiven) -> TypeIs[T]:
    """Return ``True`` unless ``value`` is the ``NOT_GIVEN`` sentinel."""
    return not isinstance(value, NotGiven)


type Timeout = httpx2.Timeout
type TimeoutTypes = float | httpx2.Timeout | None
type Headers = Mapping[str, str | NotGiven]
type Query = Mapping[str, object]
type Body = Mapping[str, object]

type FileContent = IO[bytes] | bytes | os.PathLike[str]
type FileTypes = (
    FileContent
    | tuple[str | None, FileContent]
    | tuple[str | None, FileContent, str | None]
    | tuple[str | None, FileContent, str | None, Mapping[str, str]]
)

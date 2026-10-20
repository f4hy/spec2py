"""Multipart uploads from file objects, paths, bytes or ``(name, content)`` tuples."""

import json
import os
import pathlib
from collections.abc import Mapping, Sequence
from typing import IO, Any, cast

from ._models import jsonable
from ._types import FileContent, FileTypes, NotGiven

__all__ = ["to_httpx_files", "to_multipart_data"]

type HttpxFile = (
    tuple[str | None, IO[bytes] | bytes, str | None]
    | tuple[str | None, IO[bytes] | bytes, str | None, Mapping[str, str]]
)


def _content(content: FileContent) -> tuple[str | None, IO[bytes] | bytes]:
    if isinstance(content, os.PathLike):
        path = pathlib.Path(content)
        return path.name, path.read_bytes()
    if isinstance(content, bytes):
        return None, content
    name = getattr(content, "name", None)
    return (pathlib.Path(name).name if isinstance(name, str) else None), content


def _one(file: FileTypes) -> HttpxFile:
    if isinstance(file, tuple):
        parts = cast(tuple[Any, ...], file)
        name, content = parts[0], parts[1]
        default_name, body = _content(content)
        content_type = parts[2] if len(parts) > 2 else None
        filename = name if name is not None else default_name
        if len(parts) > 3:
            return (filename, body, content_type, parts[3])
        return (filename, body, content_type)
    default_name, body = _content(file)
    return (default_name or "upload", body, None)


def to_httpx_files(
    files: Mapping[str, FileTypes | Sequence[FileTypes] | NotGiven | None],
) -> list[tuple[str, HttpxFile]]:
    """Normalize generated-client file arguments into httpx2's ``files=`` shape.

    ``None`` (an optional file left out) is dropped like ``NOT_GIVEN``.
    """
    out: list[tuple[str, HttpxFile]] = []
    for field, value in files.items():
        if isinstance(value, NotGiven) or value is None:
            continue
        if isinstance(value, list):
            out.extend((field, _one(item)) for item in value)
        else:
            out.append((field, _one(cast(FileTypes, value))))
    return out


def to_multipart_data(fields: Mapping[str, object]) -> dict[str, str | list[str]]:
    """Encode non-file form fields: scalars as text, objects as JSON."""
    data: dict[str, str | list[str]] = {}
    for key, value in fields.items():
        if isinstance(value, NotGiven) or value is None:
            continue
        if isinstance(value, bool):
            data[key] = "true" if value else "false"
        elif isinstance(value, str | int | float):
            data[key] = str(value)
        elif isinstance(value, list) and all(
            isinstance(v, str | int | float) for v in cast(list[object], value)
        ):
            data[key] = [str(v) for v in cast(list[object], value)]
        else:
            encoded = jsonable(cast(object, value))
            data[key] = encoded if isinstance(encoded, str) else json.dumps(encoded)
    return data

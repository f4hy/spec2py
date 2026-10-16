"""Rewrite OpenAPI 3.0 schema dialect into 3.1 (JSON Schema 2020-12) semantics.

After this pass every later stage can assume 3.1: ``nullable`` is gone (folded
into ``type`` arrays or an ``anyOf`` with ``null``), boolean
``exclusiveMinimum``/``exclusiveMaximum`` are numeric, and ``example`` sits next
to ``examples``.
"""

from typing import Any, cast

__all__ = ["upgrade_to_31"]

type JSON = Any


def _upgrade_schema(node: dict[str, JSON]) -> dict[str, JSON]:
    s = dict(node)
    nullable = s.pop("nullable", None)
    for bound, limit in (
        ("exclusiveMinimum", "minimum"),
        ("exclusiveMaximum", "maximum"),
    ):
        if isinstance(s.get(bound), bool):
            if s[bound] and limit in s:
                s[bound] = s.pop(limit)
            else:
                del s[bound]
    if nullable is True:
        t = s.get("type")
        if isinstance(t, str):
            s["type"] = [t, "null"]
            if "enum" in s and None not in s["enum"]:
                s["enum"] = [*s["enum"], None]
        elif isinstance(t, list):
            if "null" not in t:
                s["type"] = [*cast(list[str], t), "null"]
        else:
            keep = {
                k: v
                for k, v in s.items()
                if k
                in (
                    "description",
                    "title",
                    "default",
                    "deprecated",
                    "readOnly",
                    "writeOnly",
                )
            }
            inner = {k: v for k, v in s.items() if k not in keep}
            s = {**keep, "anyOf": [inner, {"type": "null"}]}
    return s


def _walk(node: JSON) -> JSON:
    if isinstance(node, dict):
        d = cast(dict[str, JSON], node)
        if isinstance(d.get("nullable"), bool) or any(
            isinstance(d.get(k), bool) for k in ("exclusiveMinimum", "exclusiveMaximum")
        ):
            d = _upgrade_schema(d)
        return {k: _walk(v) for k, v in d.items()}
    if isinstance(node, list):
        return [_walk(v) for v in cast(list[JSON], node)]
    return node


def upgrade_to_31(document: dict[str, JSON]) -> dict[str, JSON]:
    """Return ``document`` rewritten with 3.1 schema semantics."""
    version = str(document.get("openapi", ""))
    if not version.startswith("3.0"):
        return document
    upgraded = cast(dict[str, JSON], _walk(document))
    upgraded["openapi"] = "3.1.0"
    return upgraded

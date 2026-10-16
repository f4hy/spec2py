"""Apply OpenAPI Overlay documents (1.0, plus 1.1's ``copy``) to a spec.

An overlay is a list of actions, each targeting nodes by JSONPath::

    overlay: 1.0.0
    info: {title: Fix vendor spec, version: 1.0.0}
    actions:
      - target: $.paths['/invoices'].get
        update: {operationId: listInvoices}
      - target: $.paths['/internal']
        remove: true

``update`` deep-merges into objects and appends to arrays; ``remove`` deletes
the targets; ``copy`` (1.1) copies the value at another path into the target.
"""

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from .jsonpath import JSONPathError, find

__all__ = ["OverlayError", "OverlayReport", "apply_overlay", "deep_merge"]

type JSON = Any


class OverlayError(ValueError):
    pass


@dataclass(slots=True)
class OverlayReport:
    """What an overlay did, for ``--verbose`` output and tests."""

    applied: int = 0
    unmatched: list[str] = field(default_factory=list[str])


def deep_merge(target: JSON, update: JSON) -> JSON:
    """Merge ``update`` into ``target`` in place and return the result."""
    if isinstance(target, dict) and isinstance(update, dict):
        t = cast(dict[str, JSON], target)
        for key, value in cast(dict[str, JSON], update).items():
            if key in t and isinstance(t[key], dict | list) and isinstance(value, dict):
                t[key] = deep_merge(t[key], value)
            else:
                t[key] = copy.deepcopy(value)
        return t
    if isinstance(target, list):
        items = cast(list[JSON], target)
        if isinstance(update, list):
            items.extend(copy.deepcopy(cast(list[JSON], update)))
        else:
            items.append(copy.deepcopy(update))
        return items
    return copy.deepcopy(update)


def apply_overlay(
    document: dict[str, JSON], overlay: Mapping[str, JSON]
) -> OverlayReport:
    """Apply ``overlay``'s actions to ``document`` in order, in place."""
    if "overlay" not in overlay:
        raise OverlayError("not an Overlay document: missing the 'overlay' field")
    report = OverlayReport()
    for index, action in enumerate(cast(list[Mapping[str, JSON]], overlay.get("actions") or [])):
        target = action.get("target")
        if not isinstance(target, str):
            raise OverlayError(f"action {index} has no target")
        try:
            locations = find(document, target)
        except JSONPathError as exc:
            raise OverlayError(f"action {index}: {exc}") from exc
        if not locations:
            report.unmatched.append(target)
            continue
        if action.get("remove"):
            # Delete from the end so earlier list indices stay valid.
            for loc in sorted(locations, key=lambda l: l.path, reverse=True):
                if loc.parent is None or loc.key is None:
                    raise OverlayError("cannot remove the document root")
                del loc.parent[loc.key]
        elif "copy" in action:
            sources = find(document, cast(str, action["copy"]))
            if len(sources) != 1:
                raise OverlayError(
                    f"action {index}: copy source must match exactly one node"
                )
            for loc in locations:
                merged = deep_merge(loc.value, copy.deepcopy(sources[0].value))
                if loc.parent is not None:
                    loc.set(merged)
        elif "update" in action:
            for loc in locations:
                # Objects and arrays merge in place (including the root);
                # scalars are replaced through their parent.
                merged = deep_merge(loc.value, action["update"])
                if loc.parent is not None:
                    loc.set(merged)
        report.applied += 1
    return report

"""Load stage: read a spec from a file, a URL or a FastAPI app, then apply overlays."""

import datetime
import importlib
import json
import pathlib
import re
import sys
from collections.abc import Sequence
from typing import Any, cast

import httpx2
import yaml

from .overlay import OverlayReport, apply_overlay

__all__ = [
    "LoadError",
    "is_app_reference",
    "load_document",
    "load_spec",
    "parse_document",
]

type JSON = Any

_APP_REF = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*(\(\))?$")


class LoadError(ValueError):
    pass


def is_app_reference(source: str) -> bool:
    """``"myapp.main:app"`` (or ``"pkg.factory:create_app()"``) rather than a path or URL."""
    return bool(_APP_REF.match(source)) and not pathlib.Path(source).exists()


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def _plain(value: JSON) -> JSON:
    """YAML gives int keys (``200:``) and dates; OpenAPI wants strings."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, list):
        return [_plain(v) for v in cast(list[Any], value)]
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def parse_document(text: str, name: str = "<spec>") -> JSON:
    """Parse JSON or YAML text."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        return _plain(yaml.load(text, Loader=loader))  # noqa: S506 - safe loader
    except yaml.YAMLError as exc:
        raise LoadError(f"{name}: not valid JSON or YAML: {exc}") from exc


def fetch_text(url: str) -> str:
    try:
        response = httpx2.get(url, follow_redirects=True, timeout=60)
        response.raise_for_status()
    except httpx2.HTTPError as exc:
        raise LoadError(f"could not fetch {url}: {exc}") from exc
    return response.text


def load_document(source: str) -> JSON:
    """Load a JSON/YAML document from a path or URL."""
    if is_url(source):
        return parse_document(fetch_text(source), source)
    path = pathlib.Path(source)
    if not path.is_file():
        raise LoadError(f"spec not found: {source}")
    return parse_document(path.read_text(encoding="utf-8"), source)


def load_fastapi_app(reference: str) -> JSON:
    """Import ``module:attr`` and return its ``openapi()`` document."""
    module_name, _, attr = reference.partition(":")
    call = attr.endswith("()")
    attr = attr.removesuffix("()")
    cwd = str(pathlib.Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LoadError(f"could not import {module_name!r}: {exc}") from exc
    obj: Any = module
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise LoadError(f"{module_name!r} has no attribute {attr!r}") from exc
    if call:
        obj = obj()
    openapi = getattr(obj, "openapi", None)
    if not callable(openapi):
        raise LoadError(f"{reference} is not a FastAPI (or Starlette+openapi) app")
    return openapi()


def load_spec(
    source: str, overlays: Sequence[str] = ()
) -> tuple[dict[str, JSON], str, list[OverlayReport]]:
    """Load a spec and apply overlays. Returns ``(document, base_uri, reports)``.

    ``base_uri`` is where relative ``$ref``s resolve from.
    """
    if is_app_reference(source):
        document = load_fastapi_app(source)
        base = str(pathlib.Path.cwd() / "openapi.json")
    else:
        document = load_document(source)
        base = source if is_url(source) else str(pathlib.Path(source).resolve())
    if not isinstance(document, dict):
        raise LoadError(f"{source}: the document root must be an object")
    spec = cast(dict[str, JSON], document)
    reports: list[OverlayReport] = []
    for overlay_source in overlays:
        overlay = load_document(overlay_source)
        if not isinstance(overlay, dict):
            raise LoadError(f"{overlay_source}: overlay root must be an object")
        reports.append(apply_overlay(spec, cast(dict[str, JSON], overlay)))
    return spec, base, reports

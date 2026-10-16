"""Normalize stage: every input dialect becomes one bundled OpenAPI 3.1 document."""

from typing import Any

from .refs import RefError, bundle, resolve_pointer
from .swagger2 import swagger2_to_openapi3
from .upgrade import upgrade_to_31

__all__ = [
    "NormalizeError",
    "RefError",
    "bundle",
    "normalize",
    "resolve_pointer",
    "swagger2_to_openapi3",
    "upgrade_to_31",
]


class NormalizeError(ValueError):
    pass


def normalize(document: dict[str, Any], base_uri: str) -> dict[str, Any]:
    """Return ``document`` as bundled OpenAPI 3.1, whatever version it started as."""
    if str(document.get("swagger", "")).startswith("2"):
        document = swagger2_to_openapi3(document)
    version = str(document.get("openapi", ""))
    if not version.startswith("3."):
        raise NormalizeError(
            f"unsupported spec version {version or document.get('swagger')!r}; "
            "expected Swagger 2.0 or OpenAPI 3.0, 3.1 or 3.2"
        )
    document = bundle(document, base_uri)
    return upgrade_to_31(document)

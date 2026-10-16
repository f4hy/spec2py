"""Generator configuration, read from ``[tool.spec2py]`` in ``pyproject.toml``.

::

    [tool.spec2py]
    spec = "https://api.acme.com/openapi.json"
    output = "clients/acme"
    package = "acme"
    client-name = "Acme"
    overlays = ["overlays/fixes.yaml"]
    group-by = "tag"                # or "path"
    exclude-paths = ["/internal/*"]
    idempotency-header = "Idempotency-Key"

    [tool.spec2py.rename-models]
    InvoiceDTO = "Invoice"

    [tool.spec2py.operations."GET /invoices"]
    name = "list"
    resource = "invoices"
    pagination = { style = "cursor", items = "data", cursor-param = "after", next-cursor = "last_id" }

    [tool.spec2py.operations.createCompletion]
    stream = "sse"

Keys may be written kebab-case or snake_case.
"""

import dataclasses
import pathlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .loader import is_url

__all__ = [
    "Config",
    "ConfigError",
    "OperationConfig",
    "PaginationConfig",
    "load_config",
]

type PaginationStyle = Literal["cursor", "offset", "page", "link"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class PaginationConfig:
    """Explicit pagination for one operation (overrides detection)."""

    style: PaginationStyle
    items: str | None = "data"
    cursor_param: str = "cursor"
    next_cursor: str | None = "next_cursor"
    cursor_from_item: str | None = None
    has_more: str | None = None
    offset_param: str = "offset"
    limit_param: str | None = "limit"
    page_param: str = "page"
    total: str | None = None
    total_pages: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationConfig:
    """Per-operation overrides, keyed by operationId or ``"METHOD /path"``."""

    name: str | None = None
    resource: str | None = None
    pagination: PaginationConfig | Literal[False] | None = None
    stream: Literal["sse", "ndjson"] | Literal[False] | None = None
    exclude: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    spec: str | None = None
    output: pathlib.Path = pathlib.Path("client")
    package: str | None = None
    client_name: str | None = None
    project_name: str | None = None
    package_version: str = "0.1.0"
    layout: Literal["project", "package"] = "project"
    overlays: tuple[str, ...] = ()
    group_by: Literal["tag", "path"] = "tag"
    base_url: str | None = None
    env_prefix: str | None = None
    idempotency_header: str | None = None
    stream_terminator: str | None = "[DONE]"
    exclude_paths: tuple[str, ...] = ()
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    rename_models: Mapping[str, str] = field(default_factory=dict[str, str])
    operations: Mapping[str, OperationConfig] = field(
        default_factory=dict[str, OperationConfig]
    )
    custom_resources: Mapping[str, str] = field(default_factory=dict[str, str])
    base_dir: pathlib.Path = pathlib.Path()

    def replace(self, **changes: Any) -> Config:
        return dataclasses.replace(self, **changes)

    def operation(
        self, operation_id: str | None, method: str, path: str
    ) -> OperationConfig:
        by_route = self.operations.get(f"{method.upper()} {path}")
        if by_route is not None:
            return by_route
        if operation_id and operation_id in self.operations:
            return self.operations[operation_id]
        return OperationConfig()


def _snake(table: Mapping[str, Any]) -> dict[str, Any]:
    return {k.replace("-", "_"): v for k, v in table.items()}


def _check_keys(table: Mapping[str, Any], klass: type, where: str) -> None:
    known = {f.name for f in dataclasses.fields(klass)}
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _pagination(raw: Any, where: str) -> PaginationConfig | Literal[False] | None:
    if raw is None:
        return None
    if raw is False:
        return False
    if isinstance(raw, str):
        raw = {"style": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.pagination must be a table, a style name or false")
    table = _snake(cast(dict[str, Any], raw))
    _check_keys(table, PaginationConfig, f"{where}.pagination")
    if table.get("style") not in ("cursor", "offset", "page", "link"):
        raise ConfigError(
            f"{where}.pagination.style must be cursor, offset, page or link"
        )
    return PaginationConfig(**table)


def _operation(raw: Mapping[str, Any], where: str) -> OperationConfig:
    table = _snake(raw)
    _check_keys(table, OperationConfig, where)
    table["pagination"] = _pagination(table.get("pagination"), where)
    stream = table.get("stream")
    if stream is True:
        table["stream"] = "sse"
    elif stream not in (None, False, "sse", "ndjson"):
        raise ConfigError(f"{where}.stream must be 'sse', 'ndjson' or false")
    return OperationConfig(**table)


def config_from_table(table: Mapping[str, Any], base_dir: pathlib.Path) -> Config:
    data = _snake(table)
    _check_keys(data, Config, "[tool.spec2py]")
    for key in ("overlays", "exclude_paths", "include_tags", "exclude_tags"):
        if key in data:
            data[key] = tuple(data[key])
    if "output" in data:
        data["output"] = base_dir / data["output"]
    data["overlays"] = tuple(
        o if is_url(o) else str(base_dir / o) for o in data.get("overlays", ())
    )
    spec = data.get("spec")
    if spec and not is_url(spec) and (base_dir / spec).exists():
        data["spec"] = str(base_dir / spec)
    data["operations"] = {
        key: _operation(value, f"[tool.spec2py.operations.{key!r}]")
        for key, value in dict(data.get("operations", {})).items()
    }
    data["base_dir"] = base_dir
    return Config(**data)


def load_config(pyproject: pathlib.Path | None = None) -> Config:
    """Load config from ``pyproject`` (default: the nearest one upward from cwd)."""
    if pyproject is None:
        here = pathlib.Path.cwd()
        for directory in (here, *here.parents):
            candidate = directory / "pyproject.toml"
            if candidate.is_file():
                pyproject = candidate
                break
    if pyproject is None or not pyproject.is_file():
        return Config()
    with pyproject.open("rb") as f:
        document = tomllib.load(f)
    table = document.get("tool", {}).get("spec2py")
    if table is None:
        return Config(base_dir=pyproject.parent)
    return config_from_table(table, pyproject.parent)

"""Model IR: a typed, language-neutral graph of an API.

Built from a normalized OpenAPI 3.1 document. Every later stage reads only the
IR, never raw YAML, so the IR can be snapshot-tested on its own
(``spec2py ir spec.yaml`` prints it as JSON).
"""

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "API",
    "Discriminator",
    "Kind",
    "Operation",
    "Parameter",
    "Property",
    "RequestBody",
    "Response",
    "Schema",
    "SecurityScheme",
    "Server",
    "to_jsonable",
]

type JSON = Any
type ParamLocation = Literal["path", "query", "header", "cookie"]


class Kind(enum.StrEnum):
    OBJECT = "object"
    ARRAY = "array"
    MAP = "map"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"
    ANY = "any"
    ENUM = "enum"
    CONST = "const"
    UNION = "union"
    REF = "ref"


@dataclass(slots=True)
class Discriminator:
    property: str
    mapping: dict[str, str] = field(default_factory=dict[str, str])
    """Wire value -> component schema name."""


@dataclass(slots=True)
class Property:
    name: str
    schema: Schema
    required: bool = False


@dataclass(slots=True, eq=False)
class Schema:
    """One schema node. ``kind`` says which of the other fields matter."""

    kind: Kind
    nullable: bool = False
    ref: str | None = None
    """Component name, for ``Kind.REF``."""
    title: str | None = None
    description: str | None = None
    deprecated: bool = False
    read_only: bool = False
    write_only: bool = False
    format: str | None = None
    default: JSON = None
    has_default: bool = False
    examples: list[JSON] = field(default_factory=list[JSON])
    properties: list[Property] = field(default_factory=list[Property])
    additional: Schema | None = None
    """Typed ``additionalProperties`` (objects and maps)."""
    bases: list[str] = field(default_factory=list[str])
    """Component names this object extends (from ``allOf``)."""
    items: Schema | None = None
    values: list[JSON] = field(default_factory=list[JSON])
    """Enum members, or the single ``const`` value."""
    variants: list[Schema] = field(default_factory=list["Schema"])
    discriminator: Discriminator | None = None
    union_kind: Literal["oneOf", "anyOf", "type"] | None = None
    content_media_type: str | None = None
    lossy: list[str] = field(default_factory=list[str])
    """Constructs Python can't express, recorded for NOTES.md."""
    extensions: dict[str, JSON] = field(default_factory=dict[str, JSON])

    def property(self, name: str) -> Property | None:
        return next((p for p in self.properties if p.name == name), None)


@dataclass(slots=True)
class Parameter:
    name: str
    location: ParamLocation
    schema: Schema
    required: bool = False
    description: str | None = None
    deprecated: bool = False
    style: str | None = None
    explode: bool | None = None


@dataclass(slots=True)
class RequestBody:
    media_type: str
    schema: Schema
    required: bool = False
    description: str | None = None
    media_types: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class Response:
    status: str
    description: str | None = None
    media_type: str | None = None
    schema: Schema | None = None
    item_schema: Schema | None = None
    """OpenAPI 3.2 ``itemSchema`` for sequential media types (SSE, NDJSON)."""
    media_types: list[str] = field(default_factory=list[str])
    headers: dict[str, Schema] = field(default_factory=dict[str, Schema])

    @property
    def is_success(self) -> bool:
        return self.status.startswith("2")


@dataclass(slots=True)
class Operation:
    method: str
    path: str
    operation_id: str | None = None
    tags: list[str] = field(default_factory=list[str])
    summary: str | None = None
    description: str | None = None
    deprecated: bool = False
    parameters: list[Parameter] = field(default_factory=list[Parameter])
    request_body: RequestBody | None = None
    responses: list[Response] = field(default_factory=list[Response])
    security: list[dict[str, list[str]]] | None = None
    """``None`` inherits the API default; ``[]`` means no auth."""
    extensions: dict[str, JSON] = field(default_factory=dict[str, JSON])

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass(slots=True)
class SecurityScheme:
    name: str
    type: Literal["apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"]
    scheme: str | None = None
    """For ``http``: ``bearer``, ``basic``..."""
    location: Literal["header", "query", "cookie"] | None = None
    parameter_name: str | None = None
    token_url: str | None = None
    flows: list[str] = field(default_factory=list[str])
    scopes: list[str] = field(default_factory=list[str])
    description: str | None = None


@dataclass(slots=True)
class Server:
    url: str
    description: str | None = None


@dataclass(slots=True)
class API:
    title: str
    version: str
    openapi_version: str
    description: str | None = None
    servers: list[Server] = field(default_factory=list[Server])
    schemas: dict[str, Schema] = field(default_factory=dict[str, Schema])
    operations: list[Operation] = field(default_factory=list[Operation])
    security_schemes: dict[str, SecurityScheme] = field(
        default_factory=dict[str, SecurityScheme]
    )
    security: list[dict[str, list[str]]] = field(
        default_factory=list[dict[str, list[str]]]
    )
    extensions: dict[str, JSON] = field(default_factory=dict[str, JSON])

    def resolve(self, schema: Schema) -> Schema:
        """Follow ``Kind.REF`` chains to the component they name."""
        seen: set[str] = set()
        while schema.kind is Kind.REF and schema.ref in self.schemas:
            assert schema.ref is not None
            if schema.ref in seen:
                break
            seen.add(schema.ref)
            schema = self.schemas[schema.ref]
        return schema


def _empty(value: object) -> bool:
    if value is None or value is False:
        return True
    return isinstance(value, list | dict) and not value


def to_jsonable(value: Any) -> JSON:
    """Dump IR to plain JSON, omitting empty/default fields for compact snapshots."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, JSON] = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            keep_default = f.name == "default" and getattr(value, "has_default", False)
            if _empty(v) and not keep_default:
                continue
            out[f.name] = to_jsonable(v)
        return out
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value

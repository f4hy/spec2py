"""Build the Model IR from a normalized OpenAPI 3.1 document."""

import re
from collections.abc import Mapping
from typing import Any, Final, Literal, cast

from .ir import (
    API,
    Discriminator,
    Kind,
    Operation,
    Parameter,
    ParamLocation,
    Property,
    RequestBody,
    Response,
    Schema,
    SecurityScheme,
    Server,
)
from .normalize.refs import RefError, resolve_pointer, unescape_pointer

__all__ = ["build_ir"]

type JSON = Any
type Obj = dict[str, JSON]

METHODS: Final = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
    "query",
)
_IGNORED_HEADERS: Final = frozenset({"accept", "content-type", "authorization"})
_PRIMITIVES: Final = {
    "string": Kind.STRING,
    "integer": Kind.INTEGER,
    "number": Kind.NUMBER,
    "boolean": Kind.BOOLEAN,
}
_STRUCTURAL: Final = frozenset(
    {
        "type",
        "properties",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "oneOf",
        "anyOf",
        "allOf",
        "required",
        "patternProperties",
        "prefixItems",
    }
)


def _is_json(media: str) -> bool:
    base = media.split(";", maxsplit=1)[0].strip().lower()
    return base == "application/json" or base.endswith("+json") or base == "*/*"


def media_rank(media: str) -> int:
    """Lower is preferred when an operation offers several media types."""
    base = media.split(";", maxsplit=1)[0].strip().lower()
    if base == "application/json":
        return 0
    if base.endswith("+json"):
        return 1
    if base in ("multipart/form-data",):
        return 2
    if base == "application/x-www-form-urlencoded":
        return 3
    if base in (
        "text/event-stream",
        "application/x-ndjson",
        "application/jsonl",
        "application/json-seq",
        "application/stream+json",
    ):
        return 4
    if base == "application/octet-stream":
        return 5
    if base.startswith("text/"):
        return 6
    return 7


def _pascal_tokens(*tokens: str) -> str:
    words = [w for t in tokens for w in re.split(r"[^A-Za-z0-9]+", t) if w]
    return "".join(w[:1].upper() + w[1:] for w in words) or "Schema"


class _Builder:
    def __init__(self, document: Obj) -> None:
        self.doc = document
        components = cast(Obj, document.get("components") or {})
        self.raw_schemas = dict(cast(Obj, components.get("schemas") or {}))
        self.api = API(
            title=str(cast(Obj, document.get("info") or {}).get("title") or "API"),
            version=str(cast(Obj, document.get("info") or {}).get("version") or "0"),
            openapi_version=str(document.get("openapi", "3.1.0")),
            description=cast(Obj, document.get("info") or {}).get("description"),
        )
        self.synthetic: dict[str, str] = {}
        self.building: set[str] = set()

    # -- refs --------------------------------------------------------------------

    def deref(self, node: JSON) -> JSON:
        """Resolve a non-schema ``$ref`` (parameter, response, ...) to its target."""
        seen: set[str] = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = cast(str, node["$ref"])
            if ref in seen or not ref.startswith("#"):
                break
            seen.add(ref)
            node = resolve_pointer(self.doc, ref[1:])
        return node

    def component_name(self, ref: str) -> str:
        """Name of the component a schema ``$ref`` points at, hoisting if needed."""
        if not ref.startswith("#"):
            raise RefError(f"unbundled external $ref {ref!r}")
        pointer = ref[1:]
        prefix = "/components/schemas/"
        rest = pointer.removeprefix(prefix)
        if pointer.startswith(prefix) and "/" not in rest:
            return unescape_pointer(rest)
        if pointer in self.synthetic:
            return self.synthetic[pointer]
        tokens = [unescape_pointer(t) for t in pointer.strip("/").split("/")]
        meaningful = [
            t
            for t in tokens
            if t
            not in (
                "components",
                "schemas",
                "properties",
                "content",
                "schema",
                "items",
                "paths",
                "responses",
                "application/json",
            )
        ]
        base = _pascal_tokens(*meaningful[-2:]) if meaningful else "Schema"
        name, n = base, 2
        while name in self.raw_schemas or name in self.synthetic.values():
            name, n = f"{base}{n}", n + 1
        self.synthetic[pointer] = name
        self.raw_schemas[name] = resolve_pointer(self.doc, pointer)
        return name

    def is_object_component(self, name: str, depth: int = 0) -> bool:
        raw = self.raw_schemas.get(name)
        if not isinstance(raw, dict) or depth > 20:
            return False
        node = cast(Obj, raw)
        if isinstance(node.get("$ref"), str) and len(node) == 1:
            return self.is_object_component(
                self.component_name(node["$ref"]), depth + 1
            )
        if any(k in node for k in ("oneOf", "anyOf", "enum", "const", "items")):
            return False
        t = node.get("type")
        if t not in (None, "object") and not (isinstance(t, list) and "object" in t):
            return False
        return bool(node.get("properties")) or "allOf" in node

    # -- schemas -----------------------------------------------------------------

    def component(self, name: str) -> Schema:
        if name in self.api.schemas:
            return self.api.schemas[name]
        placeholder = Schema(Kind.ANY)
        self.api.schemas[name] = placeholder
        built = self.schema(self.raw_schemas.get(name, {}))
        built.title = built.title or None
        self.api.schemas[name] = built
        return built

    def schema(self, node: JSON) -> Schema:
        if node is True or node is None:
            return Schema(Kind.ANY)
        if node is False:
            return Schema(
                Kind.ANY, lossy=["`false` schema (nothing allowed) mapped to object"]
            )
        if not isinstance(node, dict):
            return Schema(Kind.ANY)
        n = cast(Obj, node)
        built = self._schema(n)
        self._meta(built, n)
        return built

    def _meta(self, s: Schema, n: Obj) -> None:
        s.title = s.title or n.get("title")
        if isinstance(n.get("description"), str):
            s.description = n["description"]
        s.deprecated = s.deprecated or bool(n.get("deprecated"))
        s.read_only = s.read_only or bool(n.get("readOnly"))
        s.write_only = s.write_only or bool(n.get("writeOnly"))
        if "default" in n:
            s.default, s.has_default = n["default"], True
        if "examples" in n and isinstance(n["examples"], list):
            s.examples = list(cast(list[JSON], n["examples"]))
        elif "example" in n:
            s.examples = [n["example"]]
        s.extensions.update({k: v for k, v in n.items() if k.startswith("x-")})

    def _schema(self, n: Obj) -> Schema:
        if isinstance(n.get("$ref"), str):
            ref = Schema(Kind.REF, ref=self.component_name(n["$ref"]))
            rest = {k: v for k, v in n.items() if k in _STRUCTURAL}
            if rest:
                # 3.1 allows siblings next to $ref: treat as allOf.
                return self._all_of([{"$ref": n["$ref"]}, rest], {})
            return ref

        raw_type = n.get("type")
        types = (
            [raw_type]
            if isinstance(raw_type, str)
            else list(cast(list[str], raw_type or []))
        )
        nullable = "null" in types
        types = [t for t in types if t != "null"]

        if "const" in n:
            s = Schema(Kind.CONST, values=[n["const"]])
            if n["const"] is None:
                s = Schema(Kind.NULL)
        elif "enum" in n and isinstance(n["enum"], list):
            values = cast(list[JSON], n["enum"])
            nullable = nullable or None in values
            members = [v for v in values if v is not None]
            s = (
                Schema(Kind.ENUM, values=members, format=types[0] if types else None)
                if members
                else Schema(Kind.NULL)
            )
        elif "oneOf" in n or "anyOf" in n:
            s = self._union(n)
        elif "allOf" in n:
            s = self._all_of(cast(list[JSON], n["allOf"]), n)
        elif len(types) > 1:
            s = Schema(
                Kind.UNION,
                union_kind="type",
                variants=[self.schema({**n, "type": t}) for t in types],
            )
        elif types == ["object"] or (
            not types
            and any(
                k in n
                for k in ("properties", "additionalProperties", "patternProperties")
            )
        ):
            s = self._object(n)
        elif types == ["array"] or (not types and ("items" in n or "prefixItems" in n)):
            s = self._array(n)
        elif types and types[0] in _PRIMITIVES:
            s = Schema(_PRIMITIVES[types[0]], format=n.get("format"))
            if types[0] == "string" and s.format is None:
                media = n.get("contentMediaType")
                if (
                    isinstance(media, str)
                    and not _is_json(media)
                    and n.get("contentEncoding") is None
                ):
                    s.format = "binary"
                s.content_media_type = media if isinstance(media, str) else None
        elif not types and nullable:
            s = Schema(Kind.NULL)
        else:
            s = Schema(Kind.ANY)
            if not types and not any(k in n for k in _STRUCTURAL):
                s.lossy.append("untyped schema")
        if nullable and s.kind is not Kind.NULL:
            s.nullable = True
        return s

    def _union(self, n: Obj) -> Schema:
        key: Literal["oneOf", "anyOf"] = "oneOf" if "oneOf" in n else "anyOf"
        variants: list[Schema] = []
        nullable = False
        for v in cast(list[JSON], n[key]):
            built = self.schema(v)
            if built.kind is Kind.NULL:
                nullable = True
                continue
            variants.append(built)
        if "properties" in n:
            # Base properties next to oneOf: fold them into each object variant.
            base = self._object(n)
            variants = [self._merge_into(v, base) for v in variants]
        if not variants:
            return Schema(Kind.NULL)
        if len(variants) == 1:
            single = variants[0]
            single.nullable = single.nullable or nullable
            return single
        s = Schema(Kind.UNION, variants=variants, union_kind=key, nullable=nullable)
        disc = n.get("discriminator")
        if isinstance(disc, dict) and isinstance(disc.get("propertyName"), str):
            mapping = {
                str(value): self.component_name(target)
                if target.startswith("#")
                else target
                for value, target in cast(
                    dict[str, str], disc.get("mapping") or {}
                ).items()
            }
            if not mapping:
                mapping = {
                    v.ref: v.ref for v in variants if v.kind is Kind.REF and v.ref
                }
            s.discriminator = Discriminator(
                property=disc["propertyName"], mapping=mapping
            )
        return s

    def _merge_into(self, variant: Schema, base: Schema) -> Schema:
        if variant.kind is Kind.REF:
            obj = Schema(Kind.OBJECT, bases=[cast(str, variant.ref)])
            obj.properties = list(base.properties)
            return obj
        if variant.kind is Kind.OBJECT:
            names = {p.name for p in variant.properties}
            variant.properties = [
                p for p in base.properties if p.name not in names
            ] + variant.properties
        return variant

    def _all_of(self, parts: list[JSON], n: Obj) -> Schema:
        rest = {k: v for k, v in n.items() if k in _STRUCTURAL and k != "allOf"}
        if rest == {"type": "object"}:
            rest = {}  # ``type: object`` next to allOf adds nothing
        if rest:
            parts = [*parts, rest]
        if len(parts) == 1:
            return self.schema(parts[0])
        obj = Schema(Kind.OBJECT)
        required: set[str] = set()
        for part in parts:
            built = self.schema(part)
            if built.kind is Kind.REF:
                assert built.ref is not None
                if self.is_object_component(built.ref):
                    obj.bases.append(built.ref)
                    continue
                target = self.component(built.ref)
                if target.kind is Kind.ANY:
                    continue
                built = target
            if built.kind is Kind.OBJECT:
                obj.bases.extend(b for b in built.bases if b not in obj.bases)
                for prop in built.properties:
                    existing = obj.property(prop.name)
                    if existing is not None:
                        obj.properties.remove(existing)
                        prop.required = prop.required or existing.required
                    obj.properties.append(prop)
                if built.additional is not None:
                    obj.additional = built.additional
                obj.lossy.extend(built.lossy)
            elif built.kind is Kind.ANY or (
                built.kind is Kind.MAP
                and (built.additional is None or built.additional.kind is Kind.ANY)
            ):
                continue  # an untyped object member constrains nothing
            elif (
                isinstance(part, dict)
                and "required" in part
                and len(cast(Obj, part)) == 1
            ):
                required.update(cast(list[str], cast(Obj, part)["required"]))
            else:
                # allOf of non-objects (e.g. string + format): keep the most specific.
                built.lossy.append("allOf of non-object schemas narrowed to one member")
                return built
        for prop in obj.properties:
            if prop.name in required:
                prop.required = True
        if required - {p.name for p in obj.properties}:
            obj.extensions["x-apismith-required-inherited"] = sorted(
                required - {p.name for p in obj.properties}
            )
        if not obj.properties and len(obj.bases) == 1 and obj.additional is None:
            return Schema(Kind.REF, ref=obj.bases[0])
        return obj

    def _object(self, n: Obj) -> Schema:
        required = set(cast(list[str], n.get("required") or []))
        props = cast(Obj, n.get("properties") or {})
        additional = n.get("additionalProperties")
        lossy: list[str] = []
        if "patternProperties" in n:
            lossy.append("patternProperties mapped to untyped extra fields")
            if additional is None:
                additional = True
        typed_additional = (
            self.schema(additional)
            if isinstance(additional, dict) and additional
            else None
        )
        if not props:
            if "required" in n and required:
                obj = Schema(Kind.OBJECT, lossy=lossy)
                obj.properties = [
                    Property(name, Schema(Kind.ANY), True) for name in sorted(required)
                ]
                obj.additional = typed_additional
                return obj
            return Schema(
                Kind.MAP, additional=typed_additional or Schema(Kind.ANY), lossy=lossy
            )
        obj = Schema(Kind.OBJECT, lossy=lossy, additional=typed_additional)
        obj.properties = [
            Property(name, self.schema(value), name in required)
            for name, value in props.items()
        ]
        return obj

    def _array(self, n: Obj) -> Schema:
        if "prefixItems" in n:
            prefix = [self.schema(v) for v in cast(list[JSON], n["prefixItems"])]
            items = (
                prefix[0]
                if len(prefix) == 1
                else Schema(Kind.UNION, variants=prefix, union_kind="anyOf")
            )
            return Schema(
                Kind.ARRAY, items=items, lossy=["tuple (prefixItems) mapped to a list"]
            )
        return Schema(Kind.ARRAY, items=self.schema(n.get("items", True)))

    # -- operations --------------------------------------------------------------

    def parameter(self, raw: JSON) -> Parameter | None:
        p = cast(Obj, self.deref(raw))
        location = p.get("in")
        name = p.get("name")
        if location not in ("path", "query", "header", "cookie") or not isinstance(
            name, str
        ):
            return None
        if location == "header" and name.lower() in _IGNORED_HEADERS:
            return None
        if "schema" in p:
            schema = self.schema(p["schema"])
        elif isinstance(p.get("content"), dict) and p["content"]:
            media = next(iter(cast(Obj, p["content"]).values()))
            schema = self.schema(cast(Obj, media).get("schema", True))
            schema.lossy.append("content-encoded parameter sent as plain text")
        else:
            schema = Schema(Kind.STRING)
        return Parameter(
            name=name,
            location=cast(ParamLocation, location),
            schema=schema,
            required=bool(p.get("required")) or location == "path",
            description=p.get("description"),
            deprecated=bool(p.get("deprecated")),
            style=p.get("style"),
            explode=p.get("explode"),
        )

    def request_body(self, raw: JSON) -> RequestBody | None:
        body = cast(Obj, self.deref(raw))
        content = cast(Obj, body.get("content") or {})
        if not content:
            return None
        media_types = list(content)
        media = min(media_types, key=media_rank)
        media_obj = cast(Obj, content[media] or {})
        schema = self.schema(media_obj.get("schema", True))
        if media.startswith("application/octet-stream") or (
            not _is_json(media)
            and not media.startswith(
                ("multipart/", "application/x-www-form-urlencoded", "text/")
            )
        ):
            schema = Schema(Kind.STRING, format="binary")
        return RequestBody(
            media_type=media,
            schema=schema,
            required=bool(body.get("required")),
            description=body.get("description"),
            media_types=media_types,
        )

    def response(self, status: str, raw: JSON) -> Response:
        r = cast(Obj, self.deref(raw))
        content = cast(Obj, r.get("content") or {})
        response = Response(status=status.upper(), description=r.get("description"))
        response.media_types = list(content)
        if content:
            media = min(content, key=media_rank)
            media_obj = cast(Obj, content[media] or {})
            response.media_type = media
            if "schema" in media_obj:
                response.schema = self.schema(media_obj["schema"])
            if "itemSchema" in media_obj:
                response.item_schema = self.schema(media_obj["itemSchema"])
            # A streaming alternative next to JSON (``stream: true`` style APIs).
            for other in content:
                if media_rank(other) == 4 and other != media:
                    other_obj = cast(Obj, content[other] or {})
                    if "itemSchema" in other_obj:
                        response.item_schema = self.schema(other_obj["itemSchema"])
        for name, header in cast(Obj, r.get("headers") or {}).items():
            h = cast(Obj, self.deref(header))
            response.headers[name.lower()] = self.schema(
                h.get("schema", {"type": "string"})
            )
        return response

    def operations(self) -> None:
        paths = cast(Obj, self.doc.get("paths") or {})
        for path, raw_item in paths.items():
            resolved = self.deref(raw_item)
            if not isinstance(resolved, dict):
                continue
            item = cast(Obj, resolved)
            shared = [
                self.parameter(p)
                for p in cast(list[JSON], item.get("parameters") or [])
            ]
            ops: list[tuple[str, Obj]] = [
                (m, cast(Obj, item[m]))
                for m in METHODS
                if isinstance(item.get(m), dict)
            ]
            for method, op in cast(Obj, item.get("additionalOperations") or {}).items():
                ops.append((method.lower(), cast(Obj, op)))
            for method, op in ops:
                params: dict[tuple[str, str], Parameter] = {
                    (p.name, p.location): p for p in shared if p is not None
                }
                for raw in cast(list[JSON], op.get("parameters") or []):
                    if (param := self.parameter(raw)) is not None:
                        params[(param.name, param.location)] = param
                responses = [
                    self.response(str(status), raw)
                    for status, raw in cast(Obj, op.get("responses") or {}).items()
                    if not str(status).startswith("x-")
                ]
                operation = Operation(
                    method=method,
                    path=path,
                    operation_id=op.get("operationId"),
                    tags=[str(t) for t in cast(list[JSON], op.get("tags") or [])],
                    summary=op.get("summary") or item.get("summary"),
                    description=op.get("description") or item.get("description"),
                    deprecated=bool(op.get("deprecated")),
                    parameters=list(params.values()),
                    request_body=self.request_body(op["requestBody"])
                    if "requestBody" in op
                    else None,
                    responses=responses,
                    security=cast(
                        list[dict[str, list[str]]] | None, op.get("security")
                    ),
                    extensions={k: v for k, v in op.items() if k.startswith("x-")},
                )
                self.api.operations.append(operation)

    def security_schemes(self) -> None:
        components = cast(Obj, self.doc.get("components") or {})
        for name, raw in cast(Obj, components.get("securitySchemes") or {}).items():
            s = cast(Obj, self.deref(raw))
            kind = s.get("type")
            if kind not in ("apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"):
                continue
            scheme = SecurityScheme(
                name=name, type=kind, description=s.get("description")
            )
            if kind == "apiKey":
                scheme.location = s.get("in", "header")
                scheme.parameter_name = s.get("name")
            elif kind == "http":
                scheme.scheme = str(s.get("scheme", "bearer")).lower()
            elif kind == "oauth2":
                flows = cast(Obj, s.get("flows") or {})
                scheme.flows = list(flows)
                for flow in flows.values():
                    f = cast(Obj, flow)
                    scheme.token_url = scheme.token_url or f.get("tokenUrl")
                    scheme.scopes.extend(sorted(cast(Obj, f.get("scopes") or {})))
            self.api.security_schemes[name] = scheme
        self.api.security = cast(
            list[dict[str, list[str]]], self.doc.get("security") or []
        )

    def servers(self) -> None:
        for raw in cast(list[JSON], self.doc.get("servers") or []):
            server = cast(Obj, raw)
            url = str(server.get("url", ""))
            for var, spec in cast(
                Mapping[str, Obj], server.get("variables") or {}
            ).items():
                url = url.replace("{" + var + "}", str(spec.get("default", "")))
            self.api.servers.append(
                Server(url=url, description=server.get("description"))
            )

    def build(self) -> API:
        self.servers()
        self.security_schemes()
        self.operations()
        for name in list(self.raw_schemas):
            self.component(name)
        # Hoisting during the passes above may have added synthetic components.
        while missing := [n for n in self.raw_schemas if n not in self.api.schemas]:
            for name in missing:
                self.component(name)
        self.api.extensions = {k: v for k, v in self.doc.items() if k.startswith("x-")}
        return self.api


def build_ir(document: Obj) -> API:
    """Build the IR for a normalized (bundled, 3.1-semantics) document."""
    return _Builder(document).build()

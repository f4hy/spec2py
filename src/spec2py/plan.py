"""Plan stage: decide every Python-specific detail before any code is emitted.

Names (stable, collision-free, Pythonic), the schema-to-type mapping, grouping
of operations into resources, method signatures, and detection of pagination
and streaming shapes all happen here. The emitter only renders a ``Plan``.
"""

import fnmatch
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from .build_ir import media_rank
from .config import Config, OperationConfig, PaginationConfig, _pagination
from .emit.pyast import (
    MODELS,
    TAnnotated,
    TCall,
    TLiteral,
    TName,
    TNone,
    TSub,
    TUnion,
    TypeExpr,
)
from .ir import API, Kind, Operation, Parameter, Property, Response, Schema
from .naming import NameRegistry, camel_words, pascal, safe_identifier, singular, snake

__all__ = [
    "PAlias",
    "PAuth",
    "PClient",
    "PField",
    "PMethod",
    "PModel",
    "PPagination",
    "PParam",
    "PResource",
    "PStream",
    "Plan",
    "plan_api",
]

RUNTIME: Final = "spec2py_runtime"
MISSING: Final = object()

_MODEL_RESERVED: Final = frozenset(
    {
        "BaseModel",
        "Field",
        "Literal",
        "Annotated",
        "Tolerant",
        "FileTypes",
        "UUID",
        "Any",
    }
)
_FIELD_RESERVED: Final = frozenset(
    {
        "model_config",
        "model_fields",
        "model_computed_fields",
        "model_extra",
        "model_fields_set",
        "model_construct",
        "model_copy",
        "model_dump",
        "model_dump_json",
        "model_json_schema",
        "model_parametrized_name",
        "model_post_init",
        "model_rebuild",
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "dict",
        "json",
        "copy",
        "parse_obj",
        "parse_raw",
        "parse_file",
        "from_orm",
        "construct",
        "schema",
        "schema_json",
        "validate",
        "update_forward_refs",
        "to_dict",
        "to_json",
        "fields",
        "register",
    }
)
_PARAM_RESERVED: Final = frozenset(
    # Names generated method bodies use must not be shadowed by parameters.
    {
        "self",
        "extra_headers",
        "extra_query",
        "extra_body",
        "timeout",
        "cast",
        "build_body",
        "path_template",
    }
)
_CLIENT_RESERVED: Final = frozenset(
    {
        "with_raw_response",
        "with_options",
        "close",
        "base_url",
        "auth",
        "timeout",
        "max_retries",
        "request",
        "stream",
        "get_api_list",
        "hooks",
        "default_headers",
        "default_query",
        "user_agent",
        "retry_policy",
        "idempotency_header",
        "strict_response_validation",
    }
)
_RESOURCE_RESERVED: Final = frozenset({"with_raw_response", "with_options"})
_VERSION_SEGMENT: Final = re.compile(r"^(v\d+(\.\d+)*|api|rest)$", re.IGNORECASE)

_CURSOR_PARAMS: Final = (
    "cursor",
    "after",
    "starting_after",
    "page_token",
    "pagetoken",
    "next_token",
    "continuation_token",
    "continuationtoken",
    "marker",
    "start_cursor",
    "next_cursor",
    "pagination_token",
    "token",
)
_CURSOR_FIELDS: Final = (
    "next_cursor",
    "nextcursor",
    "next_page_token",
    "nextpagetoken",
    "cursor",
    "next_token",
    "nexttoken",
    "continuation_token",
    "next_marker",
    "nextmarker",
    "end_cursor",
    "endcursor",
    "next",
    "next_page",
    "nextpage",
    "next_page_url",
    "next_page_uri",
    "nextlink",
    "next_link",
    "next_url",
    "marker",
    "last_id",
    "after",
)
_ITEM_FIELDS: Final = (
    "data",
    "items",
    "results",
    "records",
    "entries",
    "values",
    "objects",
    "elements",
    "list",
    "content",
    "rows",
    "hits",
    "nodes",
)
_NESTED_PAGING: Final = (
    "meta",
    "pagination",
    "page_info",
    "pageinfo",
    "paging",
    "links",
    "_links",
    "cursor",
    "page",
)
_LIMIT_PARAMS: Final = (
    "limit",
    "per_page",
    "perpage",
    "page_size",
    "pagesize",
    "size",
    "count",
    "max_results",
    "maxresults",
    "first",
)
_SSE: Final = ("text/event-stream",)
_NDJSON: Final = (
    "application/x-ndjson",
    "application/jsonl",
    "application/json-seq",
    "application/stream+json",
    "application/x-jsonlines",
    "application/ndjson",
)


# -- plan data -------------------------------------------------------------------


@dataclass(slots=True)
class PField:
    py_name: str
    wire_name: str
    type: TypeExpr
    required: bool
    default: Any = MISSING
    doc: str | None = None
    deprecated: bool = False
    read_only: bool = False

    @property
    def alias(self) -> str | None:
        return self.wire_name if self.wire_name != self.py_name else None


@dataclass(slots=True)
class PModel:
    name: str
    fields: list[PField] = field(default_factory=list[PField])
    bases: list[str] = field(default_factory=list[str])
    doc: str | None = None
    extra: TypeExpr | None = None
    source: str = ""
    deprecated: bool = False


@dataclass(slots=True)
class PAlias:
    name: str
    type: TypeExpr
    doc: str | None = None
    source: str = ""


type ParamLocation = Literal[
    "path", "query", "header", "cookie", "body", "form", "file", "content"
]


@dataclass(slots=True)
class PParam:
    py_name: str
    wire_name: str
    location: ParamLocation
    type: TypeExpr
    required: bool
    doc: str | None = None
    deprecated: bool = False
    style: str | None = None
    explode: bool | None = None
    nullable: bool = False


@dataclass(slots=True)
class PPagination:
    strategy: str
    options: dict[str, Any]
    item_type: TypeExpr
    reason: str


@dataclass(slots=True)
class PStream:
    format: Literal["sse", "ndjson"]
    event_type: TypeExpr


type BodyMode = Literal["none", "flat", "whole", "multipart", "form", "content"]


@dataclass(slots=True)
class PMethod:
    name: str
    http_method: str
    path: str
    """Path template with Python parameter names as placeholders."""
    operation: Operation
    params: list[PParam] = field(default_factory=list[PParam])
    body_mode: BodyMode = "none"
    body_model: TypeExpr | None = None
    content_type: str | None = None
    return_type: TypeExpr = field(default_factory=TNone)
    kind: Literal["plain", "paginated", "stream"] = "plain"
    pagination: PPagination | None = None
    stream: PStream | None = None
    errors: list[tuple[int | str, TypeExpr]] = field(
        default_factory=list[tuple[int | str, TypeExpr]]
    )
    security: bool = True
    fixed_body: dict[str, Any] = field(default_factory=dict[str, Any])
    query_styles: dict[str, tuple[str, bool]] = field(
        default_factory=dict[str, tuple[str, bool]]
    )
    text_body: bool = False

    @property
    def summary(self) -> str | None:
        return self.operation.summary

    @property
    def description(self) -> str | None:
        return self.operation.description

    @property
    def deprecated(self) -> bool:
        return self.operation.deprecated


@dataclass(slots=True)
class PResource:
    attr: str
    class_name: str
    methods: list[PMethod] = field(default_factory=list[PMethod])
    custom: tuple[str, str] | None = None
    """``(module, ClassName)`` of a user subclass in ``_custom/`` to instantiate."""


@dataclass(slots=True)
class PAuth:
    kind: Literal["api_key", "bearer", "basic", "oauth2"]
    scheme: str
    params: list[tuple[str, str]]
    """``(python parameter, environment variable)`` pairs."""
    location: str | None = None
    parameter_name: str | None = None
    token_url: str | None = None
    scopes: list[str] = field(default_factory=list[str])
    description: str | None = None


@dataclass(slots=True)
class PClient:
    class_name: str
    package: str
    env_prefix: str
    base_url: str | None
    auth: list[PAuth]
    idempotency_header: str | None
    title: str
    version: str
    description: str | None

    @property
    def api_name(self) -> str:
        """``"Acme API"`` from either ``"Acme"`` or ``"Acme API"``."""
        return (
            self.title
            if self.title.lower().split()[-1:] == ["api"]
            else f"{self.title} API"
        )


@dataclass(slots=True)
class Plan:
    client: PClient
    models: list[PModel | PAlias]
    resources: list[PResource]
    notes: list[str]
    api: API
    config: Config


# -- planner ---------------------------------------------------------------------


_TITLE_NOISE: Final = frozenset(
    {"api", "apis", "client", "sdk", "rest", "openapi", "spec"}
)


def _is_title_noise(word: str) -> bool:
    """Trailing title words that don't belong in a class name ("API", "v2", "3")."""
    return (
        word in _TITLE_NOISE
        or word.isdigit()
        or re.fullmatch(r"v\d+", word) is not None
    )


def _literal_ok(values: Iterable[Any]) -> bool:
    return all(isinstance(v, str | int | bool) for v in values)


def _base_type(kind: Kind) -> TypeExpr:
    return {
        Kind.STRING: TName("str"),
        Kind.INTEGER: TName("int"),
        Kind.NUMBER: TName("float"),
        Kind.BOOLEAN: TName("bool"),
    }.get(kind, TName("object"))


_STRING_FORMATS: Final = {
    "date-time": TName("datetime", "datetime"),
    "date": TName("date", "datetime"),
    "time": TName("time", "datetime"),
    "uuid": TName("UUID", "uuid"),
}

type Ctx = Literal["field", "request", "response", "param"]


class _Planner:
    def __init__(self, api: API, config: Config) -> None:
        self.api = api
        self.config = config
        self.notes: list[str] = []
        self.model_names = NameRegistry(_MODEL_RESERVED)
        self.component_names: dict[str, str] = {}
        self.decls: dict[str, PModel | PAlias] = {}
        self.disc_values: dict[str, dict[str, list[str]]] = {}
        """component -> wire property -> discriminator values."""
        self.inline: dict[tuple[int, str], TypeExpr] = {}
        """Inline schemas already declared, so each gets exactly one model."""

    # -- naming --------------------------------------------------------------

    def name_components(self) -> None:
        for component in self.api.schemas:
            wanted = (
                self.config.rename_models.get(component) or pascal(component) or "Model"
            )
            wanted = safe_identifier(wanted)
            if wanted[0].islower():
                wanted = wanted[0].upper() + wanted[1:]
            self.component_names[component] = self.model_names.claim(
                wanted, suffix="Model"
            )

    def new_model_name(self, hint: str) -> str:
        return self.model_names.claim(
            safe_identifier(pascal(hint) or "Model"), suffix="Model"
        )

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # -- discriminators ------------------------------------------------------

    def collect_discriminators(self) -> None:
        for schema in self._all_schemas():
            if schema.kind is not Kind.UNION or schema.discriminator is None:
                continue
            for value, component in schema.discriminator.mapping.items():
                target = self.api.schemas.get(component)
                if target is None or target.kind is not Kind.OBJECT:
                    continue
                values = self.disc_values.setdefault(component, {}).setdefault(
                    schema.discriminator.property, []
                )
                if value not in values:
                    values.append(value)

    def _all_schemas(self) -> Iterator[Schema]:
        def walk(s: Schema | None) -> Iterator[Schema]:
            if s is None:
                return
            yield s
            for p in s.properties:
                yield from walk(p.schema)
            yield from walk(s.items)
            yield from walk(s.additional)
            for v in s.variants:
                yield from walk(v)

        for s in self.api.schemas.values():
            yield from walk(s)
        for op in self.api.operations:
            for p in op.parameters:
                yield from walk(p.schema)
            if op.request_body:
                yield from walk(op.request_body.schema)
            for r in op.responses:
                yield from walk(r.schema)
                yield from walk(r.item_schema)

    def discriminated(self, schema: Schema) -> bool:
        """A union pydantic can discriminate: every variant a model with the tag."""
        disc = schema.discriminator
        if disc is None:
            return False
        for v in schema.variants:
            if v.kind is not Kind.REF or v.ref not in disc.mapping.values():
                return False
            if self.api.resolve(v).kind is not Kind.OBJECT:
                return False
        return True

    # -- types ---------------------------------------------------------------

    def type_of(self, schema: Schema, hint: str, ctx: Ctx) -> TypeExpr:
        t = self._type_of(schema, hint, ctx)
        if schema.nullable and not t.is_none:
            t = TUnion.of(t, TNone())
        return t

    def _type_of(self, s: Schema, hint: str, ctx: Ctx) -> TypeExpr:
        match s.kind:
            case Kind.REF:
                assert s.ref is not None
                return TName(self.component_names[s.ref], MODELS)
            case Kind.STRING:
                if s.format == "binary":
                    return (
                        TName("FileTypes", RUNTIME)
                        if ctx in ("request", "param")
                        else TName("bytes")
                    )
                return _STRING_FORMATS.get(s.format or "", TName("str"))
            case Kind.INTEGER | Kind.NUMBER | Kind.BOOLEAN:
                return _base_type(s.kind)
            case Kind.NULL:
                return TNone()
            case Kind.ANY:
                return TName("object")
            case Kind.ARRAY:
                assert s.items is not None
                item_hint = hint + "Item" if singular(hint) == hint else singular(hint)
                return TSub(TName("list"), (self.type_of(s.items, item_hint, ctx),))
            case Kind.MAP:
                value = s.additional or Schema(Kind.ANY)
                return TSub(
                    TName("dict"),
                    (TName("str"), self.type_of(value, hint + "Value", ctx)),
                )
            case Kind.OBJECT:
                if (key := (id(s), "object")) in self.inline:
                    return self.inline[key]
                name = self.new_model_name(hint)
                self.inline[key] = TName(name, MODELS)
                self.declare_object(name, s, source=f"inline: {hint}")
                return self.inline[key]
            case Kind.CONST:
                value = s.values[0]
                return (
                    TLiteral((value,))
                    if _literal_ok([value])
                    else _base_type(Kind.STRING)
                )
            case Kind.ENUM:
                if not _literal_ok(s.values):
                    return (
                        TName("float")
                        if all(isinstance(v, int | float) for v in s.values)
                        else TName("object")
                    )
                literal = TLiteral(tuple(s.values))
                if ctx in ("param", "request"):
                    return literal
                if (key := (id(s), "enum")) in self.inline:
                    return self.inline[key]
                name = self.new_model_name(hint)
                tolerant = TAnnotated(literal, (TName("Tolerant", RUNTIME),))
                self.decls[name] = PAlias(
                    name, tolerant, doc=s.description, source=f"inline enum: {hint}"
                )
                self.inline[key] = TName(name, MODELS)
                return self.inline[key]
            case Kind.UNION:
                return self.union_type(s, hint, ctx)

    def union_type(self, s: Schema, hint: str, ctx: Ctx) -> TypeExpr:
        members: list[TypeExpr] = []
        mapping_by_ref = {
            v: k
            for k, v in (s.discriminator.mapping.items() if s.discriminator else [])
        }
        for i, variant in enumerate(s.variants, 1):
            if variant.title:
                variant_hint = hint + pascal(variant.title)
            elif variant.ref and variant.ref in mapping_by_ref:
                variant_hint = hint + pascal(mapping_by_ref[variant.ref])
            else:
                variant_hint = f"{hint}Variant{i}"
            members.append(self.type_of(variant, variant_hint, ctx))
        union = TUnion.of(*members)
        if self.discriminated(s):
            assert s.discriminator is not None
            py = self.discriminator_py_name(s)
            return TAnnotated(
                union, (TCall(TName("Field", "pydantic"), (("discriminator", py),)),)
            )
        if s.discriminator is not None:
            self.note(
                f"`{hint}`: discriminator `{s.discriminator.property}` dropped because a variant is not a named object; validated as a plain union."
            )
        elif (
            s.union_kind != "type"
            and len(members) > 1
            and any(isinstance(m, TName) and m.module == MODELS for m in members)
        ):
            self.note(
                f"`{hint}`: union without a discriminator; pydantic picks the best-matching member (smart mode)."
            )
        return union

    def discriminator_py_name(self, s: Schema) -> str:
        assert s.discriminator is not None
        for variant in s.variants:
            target = self.api.resolve(variant)
            for prop in self.all_properties(target):
                if prop.name == s.discriminator.property:
                    return self.field_name(prop.name)
        return self.field_name(s.discriminator.property)

    def field_name(self, wire: str) -> str:
        name = safe_identifier(snake(wire) or wire, fallback="field")
        if name.startswith("_"):
            name = "field" + name
        if name in _FIELD_RESERVED:
            name = f"{name}_"
        return name

    # -- models --------------------------------------------------------------

    def all_properties(self, schema: Schema, depth: int = 0) -> list[Property]:
        """Own plus inherited properties (own ones win), resolving allOf bases."""
        props: dict[str, Property] = {}
        if depth < 20:
            for base in schema.bases:
                for p in self.all_properties(
                    self.api.resolve(Schema(Kind.REF, ref=base)), depth + 1
                ):
                    props[p.name] = p
        for p in schema.properties:
            props[p.name] = p
        return list(props.values())

    def model_base(self, component: str) -> str:
        target = self.api.schemas[component]
        seen = {component}
        while target.kind is Kind.REF and target.ref and target.ref not in seen:
            component = target.ref
            seen.add(component)
            target = self.api.schemas[component]
        return component

    def plan_field(self, owner: str, prop: Property, registry: NameRegistry) -> PField:
        py = registry.claim(self.field_name(prop.name), suffix="_")
        if py != prop.name and safe_identifier(snake(prop.name)) != snake(prop.name):
            self.note(
                f"`{owner}.{prop.name}` is exposed as `{py}` (not a valid Python name)."
            )
        ftype = self.type_of(prop.schema, owner + pascal(prop.name), "field")
        required = prop.required and not prop.schema.write_only
        default: Any = MISSING
        if not required:
            if not ftype.is_none and not (
                isinstance(ftype, TUnion) and any(m.is_none for m in ftype.members)
            ):
                ftype = TUnion.of(ftype, TNone())
            default = None
        return PField(
            py_name=py,
            wire_name=prop.name,
            type=ftype,
            required=required,
            default=default,
            doc=prop.schema.description,
            deprecated=prop.schema.deprecated,
            read_only=prop.schema.read_only,
        )

    def declare_object(
        self, name: str, s: Schema, *, source: str, component: str | None = None
    ) -> PModel:
        title = s.title if s.title and pascal(s.title) != name else None
        model = PModel(
            name=name,
            doc=s.description or title,
            source=source,
            deprecated=s.deprecated,
        )
        self.decls[name] = model
        tags = self.disc_values.get(component or "", {})
        bases = [self.model_base(b) for b in s.bases]
        # Only object components can be base classes; others contribute fields.
        copied = [
            p
            for b in bases
            if self.api.schemas[b].kind is not Kind.OBJECT
            for p in self.all_properties(self.api.schemas[b])
        ]
        bases = [b for b in bases if self.api.schemas[b].kind is Kind.OBJECT]
        parent = bases[0] if bases else None
        # Extra bases are flattened: pydantic models compose poorly with multiple
        # inheritance, and the merged model reads the same.
        for extra in bases[1:]:
            copied.extend(self.all_properties(self.api.schemas[extra]))
        if len(bases) > 1:
            self.note(
                f"`{name}`: allOf with {len(bases)} bases; inherits "
                f"`{self.component_names[bases[0]]}` and copies the fields of the rest."
            )
        own = {p.name: p for p in [*copied, *s.properties]}
        inherited: dict[str, PField] = {}
        if parent is not None:
            self.ensure_declared(parent)
            inherited = {
                f.wire_name: f for f in self.model_fields(self.component_names[parent])
            }
            # Untyped re-declarations (often only adding a description or
            # marking a field required) keep the inherited field as it is.
            own = {
                k: p
                for k, p in own.items()
                if not (k in inherited and p.schema.kind is Kind.ANY)
            }
        registry = NameRegistry(_FIELD_RESERVED)
        registry.reserve(*(f.py_name for f in inherited.values()))
        # Narrowing an inherited field (a Literal discriminator tag, or any other
        # type change) is an unsound override of a mutable attribute; when that
        # would happen the model copies its base's fields instead of inheriting.
        flatten = bool(set(tags) & set(inherited))
        fields: list[PField] = []
        for prop in own.values():
            if prop.name in inherited:
                f = self.plan_field(name, prop, NameRegistry(_FIELD_RESERVED))
                f.py_name = inherited[prop.name].py_name
                flatten = flatten or f.type != inherited[prop.name].type
            else:
                f = self.plan_field(name, prop, registry)
            fields.append(f)
        if flatten:
            overridden = {f.wire_name for f in fields}
            fields = [f for w, f in inherited.items() if w not in overridden] + fields
            parent = None
        model.bases = [self.component_names[parent]] if parent is not None else []
        model.fields = fields
        for prop_name, values in tags.items():
            wire_props = {p.name: p for p in self.all_properties(s)}
            existing = next((f for f in model.fields if f.wire_name == prop_name), None)
            tagged = PField(
                existing.py_name if existing else self.field_name(prop_name),
                prop_name,
                TLiteral(tuple(values)),
                required=len(values) > 1,
                default=values[0] if len(values) == 1 else MISSING,
                doc=wire_props[prop_name].schema.description
                if prop_name in wire_props
                else None,
            )
            if existing is not None:
                model.fields[model.fields.index(existing)] = tagged
            else:
                model.fields.append(tagged)
        for lossy in s.lossy:
            self.note(f"`{name}`: {lossy}.")
        if s.additional is not None:
            model.extra = self.type_of(s.additional, name + "Extra", "field")
        return model

    def ensure_declared(self, component: str) -> None:
        if self.component_names[component] not in self.decls:
            self.declare_component(component, self.api.schemas[component])

    def declare_component(self, component: str, s: Schema) -> None:
        name = self.component_names[component]
        source = f"#/components/schemas/{component}"
        if s.kind is Kind.OBJECT:
            self.declare_object(name, s, source=source, component=component)
            return
        if s.kind is Kind.ENUM and _literal_ok(s.values):
            t: TypeExpr = TAnnotated(
                TLiteral(tuple(s.values)), (TName("Tolerant", RUNTIME),)
            )
            if s.nullable:
                t = TUnion.of(t, TNone())
        else:
            t = self.type_of(s, name, "field")
        for lossy in s.lossy:
            self.note(f"`{name}`: {lossy}.")
        self.decls[name] = PAlias(name, t, doc=s.description or s.title, source=source)

    def ordered_models(self) -> list[PModel | PAlias]:
        ordered: list[PModel | PAlias] = []
        placed: set[str] = set()

        def place(name: str, stack: frozenset[str] = frozenset()) -> None:
            if name in placed or name in stack or name not in self.decls:
                return
            decl = self.decls[name]
            if isinstance(decl, PModel):
                for base in decl.bases:
                    place(base, stack | {name})
            placed.add(name)
            ordered.append(decl)

        for name in sorted(self.decls):
            place(name)
        return ordered

    # -- operations ----------------------------------------------------------

    def included(self, op: Operation, oc: OperationConfig) -> bool:
        if oc.exclude:
            return False
        if any(
            fnmatch.fnmatch(op.path, pattern) for pattern in self.config.exclude_paths
        ):
            return False
        if self.config.include_tags and not set(op.tags) & set(
            self.config.include_tags
        ):
            return False
        return not set(op.tags) & set(self.config.exclude_tags)

    def resource_key(self, op: Operation, oc: OperationConfig) -> str:
        if oc.resource:
            return oc.resource
        if self.config.group_by == "tag" and op.tags:
            return op.tags[0]
        segments = [s for s in op.path.split("/") if s and not s.startswith("{")]
        segments = [s for s in segments if not _VERSION_SEGMENT.match(s)] or segments
        return segments[0] if segments else "default"

    def method_base_name(self, op: Operation, resource: str) -> str:
        resource_words = set(camel_words(resource)) | {
            singular(w) for w in camel_words(resource)
        }
        path_params = {snake(p.name) for p in op.parameters if p.location == "path"}
        if op.operation_id:
            words = camel_words(op.operation_id)
            stripped = [w for w in words if w not in resource_words]
            if stripped and stripped != words:
                words = stripped
            if "by" in words[1:]:
                i = words.index("by", 1)
                if "_".join(words[i + 1 :]) in path_params | {"id"}:
                    words = words[:i]
            if words == ["get"]:
                words = ["retrieve"] if op.path.rstrip("/").endswith("}") else ["list"]
            return "_".join(words)
        # No operationId: derive from method + path relative to the resource.
        segments = [s for s in op.path.strip("/").split("/") if s]
        tail = segments[-1] if segments else ""
        is_item = tail.startswith("{")
        statics = [
            s
            for s in segments
            if not s.startswith("{") and not _VERSION_SEGMENT.match(s)
        ]
        extra = [w for s in statics for w in camel_words(s) if w not in resource_words]
        verb = {
            ("get", False): "list",
            ("get", True): "retrieve",
            ("post", False): "create",
            ("put", True): "update",
            ("patch", True): "update",
            ("delete", True): "delete",
            ("put", False): "replace",
            ("patch", False): "update",
            ("delete", False): "delete",
        }.get((op.method, is_item), op.method)
        name = (
            "_".join(extra)
            if extra and op.method == "post" and not is_item
            else "_".join([verb, *extra])
        )
        self.note(f"`{op.key}` has no operationId; its method is named `{name}`.")
        return name

    def plan_resources(self, client_name: str) -> list[PResource]:
        resources: dict[str, PResource] = {}
        attr_names = NameRegistry(_CLIENT_RESERVED)
        # Resource classes share ``_client.py`` with the client classes.
        class_names = NameRegistry(
            {
                client_name,
                f"{client_name}WithRawResponse",
                "APIClient",
                "AsyncAPIClient",
                "Hooks",
            }
        )
        by_key: dict[str, str] = {}
        method_names: dict[str, NameRegistry] = {}
        for op in self.api.operations:
            oc = self.config.operation(op.operation_id, op.method, op.path)
            if not self.included(op, oc):
                continue
            key = self.resource_key(op, oc)
            if key not in by_key:
                attr = attr_names.claim(
                    safe_identifier(snake(key) or "default"), suffix="_resource"
                )
                by_key[key] = attr
                class_name = class_names.claim(
                    safe_identifier(pascal(attr) or "Default"), suffix="Resource"
                )
                custom = None
                if attr in self.config.custom_resources:
                    module, _, klass = self.config.custom_resources[attr].partition(":")
                    custom = (module, klass)
                resources[attr] = PResource(
                    attr=attr, class_name=class_name, custom=custom
                )
                method_names[attr] = NameRegistry(_RESOURCE_RESERVED)
            attr = by_key[key]
            base = oc.name or self.method_base_name(op, key)
            name = method_names[attr].claim(safe_identifier(base or op.method))
            if name != base and base:
                full = safe_identifier(snake(op.operation_id or op.key))
                if full not in method_names[attr]:
                    name = method_names[attr].claim(full)
            for method in self.plan_operation(
                op, oc, name, resources[attr], method_names[attr]
            ):
                resources[attr].methods.append(method)
        return list(resources.values())

    def plan_operation(
        self,
        op: Operation,
        oc: OperationConfig,
        name: str,
        resource: PResource,
        names: NameRegistry,
    ) -> list[PMethod]:
        hint = singular(resource.class_name) + pascal(name)
        method = PMethod(name=name, http_method=op.method, path=op.path, operation=op)
        method.security = op.security != []
        self.plan_params(method, op, hint)
        success = sorted(
            (r for r in op.responses if r.is_success),
            key=lambda r: (r.media_type is None, r.status),
        )
        response = success[0] if success else None
        stream_format = self.stream_format(op, oc, response)
        dual = (
            stream_format is not None
            and response is not None
            and response.media_type is not None
            and media_rank(response.media_type) <= 1
        )
        if stream_format and not dual:
            if name == "create" and oc.name is None and "stream" not in names:
                names.reserve("stream")
                method.name = "stream"
            self.make_stream(method, response, stream_format, hint)
        else:
            pagination = self.detect_pagination(op, oc, response, hint)
            if pagination is not None:
                method.kind = "paginated"
                method.pagination = pagination
            else:
                method.return_type = self.return_type(response, hint + "Response")
        self.plan_errors(method, op, hint)
        methods = [method]
        if dual:
            stream_name = names.claim(
                "stream" if name == "create" else f"{name}_stream"
            )
            streaming = PMethod(
                name=stream_name, http_method=op.method, path=method.path, operation=op
            )
            streaming.security = method.security
            streaming.params = [
                p
                for p in method.params
                if not (p.location == "body" and p.wire_name == "stream")
            ]
            streaming.body_mode, streaming.body_model = (
                method.body_mode,
                method.body_model,
            )
            streaming.content_type, streaming.query_styles = (
                method.content_type,
                method.query_styles,
            )
            streaming.errors = method.errors
            if any(
                p.location == "body" and p.wire_name == "stream" for p in method.params
            ):
                streaming.fixed_body = {"stream": True}
                method.params = [
                    p
                    for p in method.params
                    if not (p.location == "body" and p.wire_name == "stream")
                ]
            assert stream_format is not None and response is not None
            self.make_stream(streaming, response, stream_format, hint)
            methods.append(streaming)
        return methods

    def plan_params(self, method: PMethod, op: Operation, hint: str) -> None:
        names = NameRegistry(_PARAM_RESERVED)
        path_order = re.findall(r"{([^}]+)}", op.path)
        params = sorted(
            op.parameters,
            key=lambda p: (
                p.location != "path",
                path_order.index(p.name) if p.name in path_order else 0,
            ),
        )
        path = op.path
        for p in params:
            py = names.claim(
                self.field_name(p.name)
                if p.location != "path"
                else safe_identifier(snake(p.name)),
                suffix=f"_{p.location}",
            )
            method.params.append(self.param(p, py, hint))
            if p.location == "path":
                path = path.replace("{" + p.name + "}", "{" + py + "}")
            elif p.location == "query" and (
                p.style not in (None, "form") or p.explode is False
            ):
                method.query_styles[p.name] = (
                    p.style or "form",
                    p.explode if p.explode is not None else p.style in (None, "form"),
                )
        method.path = path
        body = op.request_body
        if body is None:
            return
        media = body.media_type.split(";")[0].strip().lower()
        if media_rank(media) <= 1:
            self.plan_json_body(method, body.schema, body.required, hint, names)
        elif media in ("multipart/form-data", "application/x-www-form-urlencoded"):
            target = self.api.resolve(body.schema)
            method.body_mode = "multipart" if media.startswith("multipart") else "form"
            for prop in self.all_properties(target):
                if prop.schema.read_only:
                    continue
                py = names.claim(self.field_name(prop.name), suffix="_body")
                is_file = self.is_binary(prop.schema)
                ptype = self.type_of(prop.schema, hint + pascal(prop.name), "request")
                if is_file and isinstance(ptype, TSub):
                    ptype = TSub(TName("Sequence", "collections.abc"), ptype.args)
                method.params.append(
                    PParam(
                        py,
                        prop.name,
                        "file" if is_file else "form",
                        ptype,
                        prop.required,
                        prop.schema.description,
                        nullable=prop.schema.nullable,
                    )
                )
        else:
            text = media.startswith("text/")
            method.body_mode = "content"
            method.content_type = body.media_type
            method.text_body = text
            ptype = TName("str") if text else TName("bytes")
            method.params.append(
                PParam(
                    names.claim("content"),
                    "content",
                    "content",
                    ptype,
                    True,
                    body.description,
                )
            )

    def is_binary(self, s: Schema) -> bool:
        s = self.api.resolve(s)
        if s.kind is Kind.STRING and s.format == "binary":
            return True
        return s.kind is Kind.ARRAY and s.items is not None and self.is_binary(s.items)

    def plan_json_body(
        self,
        method: PMethod,
        schema: Schema,
        required: bool,
        hint: str,
        names: NameRegistry,
    ) -> None:
        target = self.api.resolve(schema)
        flat = (
            target.kind is Kind.OBJECT
            and bool(self.all_properties(target))
            and not schema.nullable
        )
        if not flat:
            method.body_mode = "whole"
            btype = self.type_of(schema, hint + "Body", "request")
            method.params.append(
                PParam(names.claim("body"), "body", "body", btype, required)
            )
            return
        method.body_mode = "flat"
        if schema.kind is Kind.REF:
            assert schema.ref is not None
            model_name = self.component_names[self.model_base(schema.ref)]
        else:
            model_name = self.new_model_name(hint + "Params")
            self.declare_object(
                model_name, target, source=f"request body: {method.operation.key}"
            )
        method.body_model = TName(model_name, MODELS)
        for f in self.model_fields(model_name):
            if f.read_only:
                continue
            prop = next(p for p in self.all_properties(target) if p.name == f.wire_name)
            ptype = self.type_of(prop.schema, model_name + pascal(prop.name), "request")
            py = names.claim(f.py_name, suffix="_body")
            param = PParam(
                py,
                f.py_name,
                "body",
                ptype,
                f.required,
                f.doc,
                f.deprecated,
                nullable=prop.schema.nullable,
            )
            method.params.append(param)

    def model_fields(self, name: str) -> list[PField]:
        decl = self.decls.get(name)
        if not isinstance(decl, PModel):
            return []
        fields: dict[str, PField] = {}
        for base in decl.bases:
            for f in self.model_fields(base):
                fields[f.wire_name] = f
        for f in decl.fields:
            fields[f.wire_name] = f
        return list(fields.values())

    def param(self, p: Parameter, py: str, hint: str) -> PParam:
        ptype = self.type_of(p.schema, hint + pascal(p.name), "param")
        return PParam(
            py_name=py,
            wire_name=p.name,
            location=p.location,
            type=ptype,
            required=p.required,
            doc=p.description,
            deprecated=p.deprecated,
            style=p.style,
            explode=p.explode,
            nullable=p.schema.nullable,
        )

    def return_type(self, response: Response | None, hint: str) -> TypeExpr:
        if response is None or response.media_type is None:
            return TNone()
        media = response.media_type.split(";")[0].strip().lower()
        if media_rank(media) <= 1:
            if response.schema is None:
                return TName("object")
            return self.type_of(response.schema, hint, "response")
        if media.startswith("text/") and not (
            response.schema and response.schema.format == "binary"
        ):
            return TName("str")
        return TName("bytes")

    def plan_errors(self, method: PMethod, op: Operation, hint: str) -> None:
        for r in op.responses:
            if (
                r.is_success
                or r.schema is None
                or r.media_type is None
                or media_rank(r.media_type) > 1
            ):
                continue
            key: int | str = (
                int(r.status)
                if r.status.isdigit()
                else ("default" if r.status == "DEFAULT" else r.status)
            )
            method.errors.append(
                (key, self.type_of(r.schema, hint + "Error", "response"))
            )

    # -- streaming -----------------------------------------------------------

    def stream_format(
        self, op: Operation, oc: OperationConfig, response: Response | None
    ) -> Literal["sse", "ndjson"] | None:
        if oc.stream is False:
            return None
        if oc.stream:
            return oc.stream
        ext = op.extensions.get("x-stream") or op.extensions.get("x-spec2py-stream")
        if ext in ("sse", "ndjson"):
            return cast(Literal["sse", "ndjson"], ext)
        if response is None:
            return None
        bases = {m.split(";")[0].strip().lower() for m in response.media_types}
        if bases & set(_SSE):
            return "sse"
        if bases & set(_NDJSON):
            return "ndjson"
        return None

    def make_stream(
        self,
        method: PMethod,
        response: Response | None,
        fmt: Literal["sse", "ndjson"],
        hint: str,
    ) -> None:
        method.kind = "stream"
        schema = None
        if response is not None:
            schema = response.item_schema
            if (
                schema is None
                and response.media_type
                and media_rank(response.media_type) == 4
            ):
                schema = response.schema
            ext = method.operation.extensions.get("x-spec2py-event")
            if isinstance(ext, str) and ext in self.component_names:
                schema = Schema(Kind.REF, ref=ext)
        if (
            schema is not None
            and schema.kind is Kind.OBJECT
            and schema.property("data")
            and fmt == "sse"
        ):
            # 3.2 style: the item schema describes the SSE envelope; type its data.
            data = schema.property("data")
            assert data is not None
            schema = data.schema
        event = (
            self.type_of(schema, hint + "Event", "response")
            if schema is not None
            else TName("str")
        )
        method.stream = PStream(fmt, event)

    # -- pagination ----------------------------------------------------------

    def detect_pagination(
        self, op: Operation, oc: OperationConfig, response: Response | None, hint: str
    ) -> PPagination | None:
        if oc.pagination is False or response is None or response.schema is None:
            return None
        explicit = oc.pagination or _pagination(
            op.extensions.get("x-pagination"), op.key
        )
        body = self.api.resolve(response.schema)
        if explicit:
            return self.explicit_pagination(explicit, body, hint)
        if op.method != "get":
            return None
        query = {p.name.lower(): p.name for p in op.parameters if p.location == "query"}
        props = (
            {p.name.lower(): p for p in self.all_properties(body)}
            if body.kind is Kind.OBJECT
            else {}
        )

        # Top-level arrays: Link header or page-number paging.
        if body.kind is Kind.ARRAY and body.items is not None:
            item = self.type_of(body.items, hint + "Item", "response")
            if "link" in response.headers:
                return PPagination(
                    "LinkHeaderPagination",
                    {"items": None},
                    item,
                    "Link response header",
                )
            if "page" in query:
                return PPagination(
                    "PageNumberPagination",
                    {"items": None, "page_param": query["page"]},
                    item,
                    "`page` query parameter",
                )
            return None
        if body.kind is not Kind.OBJECT:
            return None
        items_prop = next(
            (
                props[n]
                for n in _ITEM_FIELDS
                if n in props and self.api.resolve(props[n].schema).kind is Kind.ARRAY
            ),
            None,
        )
        if items_prop is None:
            arrays = [
                p
                for p in props.values()
                if self.api.resolve(p.schema).kind is Kind.ARRAY
            ]
            items_prop = arrays[0] if len(arrays) == 1 else None
        if items_prop is None:
            return None
        array = self.api.resolve(items_prop.schema)
        assert array.items is not None
        item = self.type_of(array.items, hint + "Item", "response")
        items = items_prop.name
        has_more = self.find_field(
            body,
            (
                "has_more",
                "hasmore",
                "has_next",
                "hasnext",
                "has_next_page",
                "hasnextpage",
                "more",
            ),
        )

        cursor_param = next((query[n] for n in _CURSOR_PARAMS if n in query), None)
        if cursor_param is not None:
            next_field = self.find_field(body, _CURSOR_FIELDS)
            options: dict[str, Any] = {"items": items, "cursor_param": cursor_param}
            if next_field:
                options["next_cursor_path"] = next_field
            elif (
                cursor_param.lower() in ("starting_after", "after", "marker")
                and has_more
            ):
                options["next_cursor_path"] = None
                options["cursor_from_item"] = "id"
            else:
                return None
            if has_more:
                options["has_more"] = has_more
            return PPagination(
                "CursorPagination", options, item, f"`{cursor_param}` query parameter"
            )
        if "offset" in query:
            options = {"items": items, "offset_param": query["offset"]}
            limit = next((query[n] for n in _LIMIT_PARAMS if n in query), None)
            options["limit_param"] = limit
            if total := self.find_field(
                body,
                (
                    "total",
                    "total_count",
                    "totalcount",
                    "count",
                    "total_results",
                    "totalresults",
                ),
            ):
                options["total"] = total
            if has_more:
                options["has_more"] = has_more
            return PPagination(
                "OffsetPagination", options, item, "`offset` query parameter"
            )
        page_param = next(
            (
                query[n]
                for n in ("page", "page_number", "pagenumber", "page_num")
                if n in query
            ),
            None,
        )
        if page_param is not None:
            options = {"items": items, "page_param": page_param}
            if total_pages := self.find_field(
                body,
                (
                    "total_pages",
                    "totalpages",
                    "pages",
                    "page_count",
                    "pagecount",
                    "num_pages",
                ),
            ):
                options["total_pages"] = total_pages
            if has_more:
                options["has_more"] = has_more
            return PPagination(
                "PageNumberPagination", options, item, f"`{page_param}` query parameter"
            )
        if "link" in response.headers:
            return PPagination(
                "LinkHeaderPagination", {"items": items}, item, "Link response header"
            )
        return None

    def find_field(self, body: Schema, candidates: tuple[str, ...]) -> str | None:
        props = {p.name.lower(): p for p in self.all_properties(body)}
        for candidate in candidates:
            if candidate in props:
                return props[candidate].name
        for container in _NESTED_PAGING:
            if container in props:
                inner = self.api.resolve(props[container].schema)
                if inner.kind is Kind.OBJECT:
                    nested = {p.name.lower(): p for p in self.all_properties(inner)}
                    for candidate in candidates:
                        if candidate in nested:
                            return f"{props[container].name}.{nested[candidate].name}"
        return None

    def explicit_pagination(
        self, pc: PaginationConfig, body: Schema, hint: str
    ) -> PPagination | None:
        items_schema: Schema | None = None
        if pc.items is None or body.kind is Kind.ARRAY:
            items_schema = body.items if body.kind is Kind.ARRAY else None
        else:
            node = body
            for part in pc.items.split("."):
                prop = next(
                    (p for p in self.all_properties(node) if p.name == part), None
                )
                if prop is None:
                    node = Schema(Kind.ANY)
                    break
                node = self.api.resolve(prop.schema)
            items_schema = node.items if node.kind is Kind.ARRAY else None
        item = (
            self.type_of(items_schema, hint + "Item", "response")
            if items_schema
            else TName("object")
        )
        items = None if body.kind is Kind.ARRAY else pc.items
        match pc.style:
            case "cursor":
                options: dict[str, Any] = {
                    "items": items,
                    "cursor_param": pc.cursor_param,
                    "next_cursor_path": pc.next_cursor,
                }
                if pc.cursor_from_item:
                    options["cursor_from_item"] = pc.cursor_from_item
                    options["next_cursor_path"] = None
                strategy = "CursorPagination"
            case "offset":
                options = {
                    "items": items,
                    "offset_param": pc.offset_param,
                    "limit_param": pc.limit_param,
                }
                if pc.total:
                    options["total"] = pc.total
                strategy = "OffsetPagination"
            case "page":
                options = {"items": items, "page_param": pc.page_param}
                if pc.total_pages:
                    options["total_pages"] = pc.total_pages
                strategy = "PageNumberPagination"
            case "link":
                options = {"items": items}
                strategy = "LinkHeaderPagination"
        if pc.has_more:
            options["has_more"] = pc.has_more
        return PPagination(strategy, options, item, "configured")

    # -- client --------------------------------------------------------------

    def plan_client(self) -> PClient:
        cfg = self.config
        title = re.split(r"\s+[-\u2013\u2014:|]\s+", self.api.title)[0]
        # Split on spaces, not case, so "GitHub" stays one word ("github").
        words = [w for w in re.split(r"[^A-Za-z0-9]+", title) if w]
        while len(words) > 1 and _is_title_noise(words[-1].lower()):
            words.pop()
        if cfg.client_name:
            class_name = safe_identifier(cfg.client_name)
            words = [class_name]
        else:
            class_name = safe_identifier(
                "".join(w[0].upper() + w[1:] for w in words) or "Client"
            )
        package = cfg.package or safe_identifier(
            "_".join(w.lower() for w in words) or "client"
        )
        env_prefix = cfg.env_prefix or package.upper()
        base_url = cfg.base_url
        if base_url is None and self.api.servers:
            url = self.api.servers[0].url
            base_url = url if url.startswith(("http://", "https://")) else None
            if base_url is None:
                self.note(
                    f"Server URL `{url}` is relative; clients must pass `base_url` (or set `{env_prefix}_BASE_URL`)."
                )
        return PClient(
            class_name=class_name,
            package=package,
            env_prefix=env_prefix,
            base_url=base_url.rstrip("/") if base_url else None,
            auth=self.plan_auth(env_prefix),
            idempotency_header=cfg.idempotency_header
            or self.detect_idempotency_header(),
            title=self.api.title,
            version=self.api.version,
            description=self.api.description,
        )

    def detect_idempotency_header(self) -> str | None:
        for op in self.api.operations:
            for p in op.parameters:
                if p.location == "header" and p.name.lower() in (
                    "idempotency-key",
                    "x-idempotency-key",
                ):
                    return p.name
        return None

    def plan_auth(self, env_prefix: str) -> list[PAuth]:
        used: list[str] = []
        for requirement in [
            *self.api.security,
            *(r for op in self.api.operations for r in (op.security or [])),
        ]:
            for name in requirement:
                if name not in used and name in self.api.security_schemes:
                    used.append(name)
        if not used:
            used = sorted(self.api.security_schemes)
        schemes = [self.api.security_schemes[n] for n in used]
        tokenish = [
            s
            for s in schemes
            if s.type == "apiKey"
            or (s.type == "http" and s.scheme == "bearer")
            or s.type in ("openIdConnect",)
            or (s.type == "oauth2" and "clientCredentials" not in s.flows)
        ]
        names = NameRegistry(_CLIENT_RESERVED | {"http_client", "transport", "hooks"})
        out: list[PAuth] = []
        for s in schemes:

            def param(name: str, scheme: str = s.name) -> tuple[str, str]:
                py = names.claim(name, suffix=f"_{snake(scheme)}")
                return py, f"{env_prefix}_{py.upper()}"

            if s.type == "apiKey":
                py = (
                    "api_key"
                    if len(tokenish) == 1 or "api_key" not in names
                    else snake(s.name)
                )
                out.append(
                    PAuth(
                        "api_key",
                        s.name,
                        [param(py)],
                        location=s.location,
                        parameter_name=s.parameter_name,
                        description=s.description,
                    )
                )
            elif s.type == "http" and s.scheme == "basic":
                out.append(
                    PAuth(
                        "basic",
                        s.name,
                        [param("username"), param("password")],
                        description=s.description,
                    )
                )
            elif s.type == "http" and s.scheme != "bearer":
                self.note(
                    f"Security scheme `{s.name}` (http {s.scheme}) is not built in; pass a custom `auth=`."
                )
            elif s.type == "oauth2" and "clientCredentials" in s.flows:
                out.append(
                    PAuth(
                        "oauth2",
                        s.name,
                        [param("client_id"), param("client_secret")],
                        token_url=s.token_url,
                        scopes=s.scopes,
                        description=s.description,
                    )
                )
            elif s.type in ("http", "oauth2", "openIdConnect"):
                py = (
                    "api_key"
                    if len(tokenish) == 1
                    else ("bearer_token" if s.type == "http" else "access_token")
                )
                out.append(
                    PAuth("bearer", s.name, [param(py)], description=s.description)
                )
            else:
                self.note(
                    f"Security scheme `{s.name}` ({s.type}) is not built in; pass a custom `auth=`."
                )
        return out

    def plan(self) -> Plan:
        self.name_components()
        self.collect_discriminators()
        for component in self.api.schemas:
            self.ensure_declared(component)
        client = self.plan_client()
        resources = self.plan_resources(client.class_name)
        return Plan(
            client=client,
            models=self.ordered_models(),
            resources=resources,
            notes=self.notes,
            api=self.api,
            config=self.config,
        )


def plan_api(api: API, config: Config) -> Plan:
    return _Planner(api, config).plan()

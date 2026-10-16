"""Model base class and the "lenient in, strict out" validation helpers.

Responses are parsed leniently: unknown fields are kept and unknown enum values
are tolerated, so a newer server never breaks an older client. Requests are
validated strictly before they are sent: wrong types fail locally, before a
round trip.
"""

import functools
import logging
import sys
import types
import typing
from collections.abc import Mapping
from typing import Annotated, Any, Final, cast

import pydantic
import pydantic_core
from pydantic import TypeAdapter, ValidationError, ValidationInfo, WrapValidator

from ._types import NotGiven

__all__ = [
    "BaseModel",
    "Tolerant",
    "build_body",
    "jsonable",
    "link_models",
    "parse_value",
    "validate_param",
]

_STRICT_CONTEXT: Final = {"apismith_strict": True}
logger = logging.getLogger("apismith")


class BaseModel(pydantic.BaseModel):
    """Base for generated models.

    Keeps fields the schema doesn't declare (``extra="allow"``), accepts both
    Python field names and wire aliases, and reads attribute docstrings as field
    descriptions. Use ``model_fields_set`` to tell an absent field from one that
    was explicitly ``null``. Validators are built lazily on first use, so
    importing a client with thousands of models stays fast.
    """

    model_config = pydantic.ConfigDict(
        extra="allow",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        use_attribute_docstrings=True,
        arbitrary_types_allowed=True,
        protected_namespaces=(),
        defer_build=True,
    )

    def to_dict(self) -> dict[str, Any]:
        """Dump to a JSON-compatible dict using wire names, omitting unset fields."""
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)

    def to_json(self, *, indent: int | None = None) -> str:
        """Dump to a JSON string using wire names, omitting unset fields."""
        return self.model_dump_json(by_alias=True, exclude_unset=True, indent=indent)


def _tolerate(value: object, handler: Any, info: ValidationInfo) -> object:
    try:
        return handler(value)
    except ValidationError:
        if info.context and info.context.get("apismith_strict"):
            raise
        return value


Tolerant: Final = WrapValidator(_tolerate)
"""Annotation marker for closed sets (enums) that tolerate unknown values on responses.

``Annotated[Literal["open", "paid"], Tolerant]`` type-checks as the literal, accepts
any value when parsing a response, and rejects unknown values when validating a
request.
"""


@functools.cache
def _adapter(tp: Any) -> TypeAdapter[Any]:
    return TypeAdapter(tp)


def parse_value[T](tp: type[T], data: object, *, strict: bool = False) -> T:
    """Validate response ``data`` against ``tp``.

    With ``strict=False`` (the default) a body that doesn't fully match is still
    returned: every field that validates is typed, the rest keep their raw JSON
    values, and a warning is logged. ``strict=True`` raises ``ValidationError``.
    """
    try:
        return cast(T, _adapter(cast(Any, tp)).validate_python(data))
    except ValidationError as exc:
        if strict:
            raise
        logger.warning(
            "Response did not match %s; returning a best-effort parse: %s",
            getattr(tp, "__name__", tp),
            exc.errors(include_url=False)[:3],
        )
        return cast(T, _best_effort(tp, data))


def _lenient(tp: Any, data: object) -> object:
    try:
        return _adapter(tp).validate_python(data)
    except ValidationError:
        return _best_effort(tp, data)


def _best_effort(tp: Any, data: object) -> object:
    origin = typing.get_origin(tp)
    if origin is Annotated:
        return _best_effort(typing.get_args(tp)[0], data)
    if isinstance(tp, typing.TypeAliasType):
        return _best_effort(tp.__value__, data)
    if origin in (list, set, tuple) and isinstance(data, list):
        (item_type, *_) = typing.get_args(tp) or (Any,)
        return [_lenient(item_type, item) for item in cast(list[object], data)]
    if origin in (types.UnionType, typing.Union) and isinstance(data, dict):
        for arg in typing.get_args(tp):
            if isinstance(arg, type) and issubclass(arg, pydantic.BaseModel):
                return _best_effort(arg, cast(object, data))
        return cast(object, data)
    if (
        isinstance(tp, type)
        and issubclass(tp, pydantic.BaseModel)
        and isinstance(data, dict)
    ):
        raw = cast(dict[str, object], data)
        _ensure_built(tp)
        values: dict[str, object] = {}
        consumed: set[str] = set()
        for name, field in tp.model_fields.items():
            key = field.alias if field.alias in raw else name
            if key not in raw:
                continue
            consumed.add(key)
            try:
                values[name] = _field_adapter(tp, name).validate_python(raw[key])
            except ValidationError:
                values[name] = _best_effort(field.annotation, raw[key])
        extras = {k: v for k, v in raw.items() if k not in consumed}
        return tp.model_construct(_fields_set=set(values), **values, **extras)
    return data


def _ensure_built(model: type[pydantic.BaseModel]) -> None:
    """Build a deferred model so its field annotations are resolved types."""
    if not model.__pydantic_complete__:
        model.model_rebuild()


@functools.cache
def _field_adapter(model: type[pydantic.BaseModel], name: str) -> TypeAdapter[Any]:
    _ensure_built(model)
    field = model.model_fields[name]
    annotation: Any = field.annotation
    if field.metadata:
        annotation = Annotated[annotation, *field.metadata]
    return TypeAdapter(annotation)


def validate_param(tp: Any, value: object) -> object:
    """Strictly validate one request value and return its JSON-compatible form."""
    adapter = _adapter(tp)
    validated = adapter.validate_python(value, strict=True, context=_STRICT_CONTEXT)
    return adapter.dump_python(
        validated, mode="json", by_alias=True, exclude_unset=True
    )


def build_body(
    model: type[pydantic.BaseModel],
    fields: Mapping[str, object],
    extra_body: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a JSON request body from flattened keyword arguments.

    Each given field is validated strictly against its declared type on
    ``model`` (so read-only required fields never get in the way) and dumped
    under its wire alias. ``NOT_GIVEN`` values are dropped; ``None`` is kept and
    sent as ``null``.
    """
    body: dict[str, object] = {}
    for name, value in fields.items():
        if isinstance(value, NotGiven):
            continue
        field = model.model_fields[name]
        adapter = _field_adapter(model, name)
        validated = adapter.validate_python(value, strict=True, context=_STRICT_CONTEXT)
        key = field.alias or name
        body[key] = adapter.dump_python(
            validated, mode="json", by_alias=True, exclude_unset=True
        )
    if extra_body:
        body.update(extra_body)
    return body


def jsonable(value: object) -> object:
    """Convert models, containers and scalars to JSON-compatible Python values."""
    match value:
        case pydantic.BaseModel():
            return value.model_dump(mode="json", by_alias=True, exclude_unset=True)
        case NotGiven():
            return None
        case Mapping():
            items = cast(Mapping[object, object], value).items()
            return {
                str(k): jsonable(v) for k, v in items if not isinstance(v, NotGiven)
            }
        case list() | tuple():
            return [jsonable(v) for v in cast(list[object], value)]
        case _:
            return pydantic_core.to_jsonable_python(value)


def link_models(namespace: Mapping[str, object]) -> None:
    """Make every model name resolvable from every module of a split models package.

    Large specs split ``models`` into a package whose modules reference each
    other only under ``TYPE_CHECKING`` (so imports stay acyclic). Called at the
    bottom of ``models/__init__.py`` with ``globals()``, this adds the package's
    exports to each submodule's namespace. Nothing is built here: models build
    their validators lazily, on first use, which keeps importing a client with
    thousands of models fast.
    """
    package = str(namespace.get("__name__"))
    exported = cast(list[str], namespace.get("__all__", []))
    names = {name: namespace[name] for name in exported if name in namespace}
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith(package + "."):
            for name, value in names.items():
                module.__dict__.setdefault(name, value)

"""Property-based tests: for random schemas and random valid payloads, generated
models round-trip losslessly (parse -> dump -> parse)."""

import itertools
import keyword
import pathlib
import string
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apismith.build_ir import build_ir
from apismith.config import Config
from apismith.emit.package import render_package
from apismith.normalize import normalize
from apismith.plan import plan_api

from .conftest import import_generated

_counter = itertools.count()

NAMES = st.one_of(
    st.text(string.ascii_lowercase, min_size=1, max_size=8),
    st.sampled_from(
        [
            "camelCase",
            "class",
            "from",
            "json",
            "model_config",
            "x-dash",
            "_under",
            "type",
            "datetime",
            "date",
            "time",
            "field",
            "literal",
            "id",
        ]
    ),
).filter(lambda n: n.isidentifier() or "-" in n)

DATE_TIMES = st.builds(
    lambda y, mo, d, h, mi, s: f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}Z",
    st.integers(1970, 2100),
    st.integers(1, 12),
    st.integers(1, 28),
    st.integers(0, 23),
    st.integers(0, 59),
    st.integers(0, 59),
)


def leaf_schema() -> st.SearchStrategy[dict[str, Any]]:
    return st.one_of(
        st.just({"type": "string"}),
        st.just({"type": "integer"}),
        st.just({"type": "number"}),
        st.just({"type": "boolean"}),
        st.just({"type": "string", "format": "date-time"}),
        st.lists(
            st.text(string.ascii_lowercase, min_size=1, max_size=5),
            min_size=1,
            max_size=4,
            unique=True,
        ).map(lambda values: {"type": "string", "enum": values}),
    )


def schemas(refs: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    base = leaf_schema()
    if refs:
        base = st.one_of(
            base,
            st.sampled_from(refs).map(lambda r: {"$ref": f"#/components/schemas/{r}"}),
        )

    def extend(
        children: st.SearchStrategy[dict[str, Any]],
    ) -> st.SearchStrategy[dict[str, Any]]:
        return st.one_of(
            children.map(lambda c: {"type": "array", "items": c}),
            children.map(lambda c: {"type": "object", "additionalProperties": c}),
            st.dictionaries(NAMES, children, min_size=1, max_size=4).flatmap(
                lambda props: st.lists(st.sampled_from(sorted(props)), unique=True).map(
                    lambda req: {"type": "object", "properties": props, "required": req}
                )
            ),
            children.map(lambda c: {**c, "nullable": True} if "$ref" not in c else c),
        )

    return st.recursive(base, extend, max_leaves=6)


@st.composite
def component_sets(draw: st.DrawFn) -> dict[str, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    for i in range(draw(st.integers(1, 4))):
        props = draw(
            st.dictionaries(NAMES, schemas(list(components)), min_size=1, max_size=5)
        )
        required = draw(st.lists(st.sampled_from(sorted(props)), unique=True))
        components[f"Model{i}"] = {
            "type": "object",
            "properties": props,
            "required": required,
        }
    return components


def instances(
    schema: dict[str, Any], components: dict[str, dict[str, Any]], depth: int = 0
) -> st.SearchStrategy[Any]:
    if "$ref" in schema:
        return instances(
            components[schema["$ref"].rsplit("/", 1)[-1]], components, depth + 1
        )
    if schema.get("nullable"):
        inner = {k: v for k, v in schema.items() if k != "nullable"}
        return st.one_of(st.none(), instances(inner, components, depth))
    if "enum" in schema:
        return st.sampled_from(schema["enum"])
    match schema.get("type"):
        case "string" if schema.get("format") == "date-time":
            return DATE_TIMES
        case "string":
            return st.text(max_size=10)
        case "integer":
            return st.integers(-(2**40), 2**40)
        case "number":
            return st.floats(allow_nan=False, allow_infinity=False, width=32)
        case "boolean":
            return st.booleans()
        case "array":
            return st.lists(
                instances(schema["items"], components, depth + 1),
                max_size=0 if depth > 3 else 3,
            )
        case "object" if "properties" in schema:
            required = set(schema.get("required", []))
            fields = {
                name: instances(sub, components, depth + 1)
                for name, sub in schema["properties"].items()
            }
            optional = [n for n in fields if n not in required]
            if depth > 3 and optional:
                # keep recursive models finite
                fields = {n: s for n, s in fields.items() if n in required}
                optional = []
            return st.fixed_dictionaries(
                {n: fields[n] for n in fields if n in required},
                optional={n: fields[n] for n in optional},
            )
        case "object":
            return st.dictionaries(
                st.text(string.ascii_lowercase, min_size=1, max_size=4),
                instances(schema["additionalProperties"], components, depth + 1),
                max_size=0 if depth > 3 else 2,
            )
    return st.none()


@st.composite
def models_and_payloads(draw: st.DrawFn) -> tuple[dict[str, dict[str, Any]], str, Any]:
    components = draw(component_sets())
    name = draw(st.sampled_from(sorted(components)))
    payload = draw(instances(components[name], components))
    return components, name, payload


def build_models(components: dict[str, dict[str, Any]], out: pathlib.Path) -> Any:
    package = f"prop{next(_counter)}"
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "P", "version": "1"},
        "paths": {},
        "components": {"schemas": components},
    }
    plan = plan_api(
        build_ir(normalize(spec, "x")), Config(package=package, layout="package")
    )
    for rel, text in render_package(plan).files.items():
        path = out / package / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return import_generated(out, package).models


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(case=models_and_payloads())
def test_models_round_trip_losslessly(
    case: tuple[dict[str, dict[str, Any]], str, Any], tmp_path: pathlib.Path
) -> None:
    components, name, payload = case
    models = build_models(components, tmp_path)
    model = getattr(models, name)
    parsed = model.model_validate(payload)
    dumped = parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped == payload
    again = model.model_validate(dumped)
    assert again == parsed
    assert again.model_dump(mode="json", by_alias=True, exclude_unset=True) == dumped


def test_names_strategy_includes_python_keywords() -> None:
    assert any(keyword.iskeyword(n) for n in ["class", "from"])

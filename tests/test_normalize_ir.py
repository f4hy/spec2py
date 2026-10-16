import json
import os
import pathlib
from typing import Any

import pytest

from apismith.build_ir import build_ir
from apismith.ir import API, Kind, to_jsonable
from apismith.loader import LoadError, load_spec, parse_document
from apismith.normalize import (
    NormalizeError,
    normalize,
    swagger2_to_openapi3,
    upgrade_to_31,
)

GOLDEN = pathlib.Path(__file__).parent / "golden"


def ir_for(spec: pathlib.Path) -> API:
    doc, base, _ = load_spec(str(spec))
    return build_ir(normalize(doc, base))


def test_yaml_gives_string_keys_and_iso_dates() -> None:
    doc = parse_document("responses:\n  200: {description: ok}\nwhen: 2024-01-02\n")
    assert doc == {"responses": {"200": {"description": "ok"}}, "when": "2024-01-02"}
    with pytest.raises(LoadError):
        parse_document("a: [unclosed")


def test_missing_spec() -> None:
    with pytest.raises(LoadError, match="not found"):
        load_spec("nope.yaml")


def test_swagger2_conversion(specs: pathlib.Path) -> None:
    doc = swagger2_to_openapi3(
        json.loads((specs / "petstore_swagger2.json").read_text())
    )
    assert doc["openapi"] == "3.0.3"
    assert doc["servers"] == [{"url": "https://petstore.example.com/v2"}]
    assert set(doc["components"]["schemas"]) == {"Pet", "Category", "ApiError"}
    assert (
        doc["components"]["schemas"]["Pet"]["properties"]["nickname"]["nullable"]
        is True
    )
    assert doc["components"]["securitySchemes"]["oauth"]["flows"]["clientCredentials"][
        "tokenUrl"
    ]
    pets = doc["paths"]["/pets"]
    tags = next(p for p in pets["get"]["parameters"] if p["name"] == "tags")
    assert tags == {
        "name": "tags",
        "in": "query",
        "schema": {"type": "array", "items": {"type": "string"}},
        "style": "form",
        "explode": False,
    }
    body = pets["post"]["requestBody"]
    assert body["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Pet"
    }
    photo = doc["paths"]["/pets/{petId}/photo"]["post"]["requestBody"]
    form = photo["content"]["multipart/form-data"]["schema"]
    assert form["properties"]["file"] == {"type": "string", "format": "binary"}
    assert form["required"] == ["file"]
    path_params = doc["paths"]["/pets/{petId}"]["get"]["parameters"]
    assert path_params == [{"$ref": "#/components/parameters/petId"}]


def test_upgrade_30_nullable_and_exclusive_bounds() -> None:
    doc = upgrade_to_31(
        {
            "openapi": "3.0.3",
            "components": {
                "schemas": {
                    "A": {"type": "string", "nullable": True, "enum": ["x"]},
                    "B": {
                        "allOf": [{"$ref": "#/components/schemas/A"}],
                        "nullable": True,
                        "description": "d",
                    },
                    "C": {
                        "type": "number",
                        "minimum": 0,
                        "exclusiveMinimum": True,
                        "maximum": 5,
                        "exclusiveMaximum": False,
                    },
                    "D": {
                        "type": "object",
                        "properties": {"nullable": {"type": "string"}},
                    },
                }
            },
        }
    )
    s = doc["components"]["schemas"]
    assert doc["openapi"] == "3.1.0"
    assert s["A"] == {"type": ["string", "null"], "enum": ["x", None]}
    assert s["B"] == {
        "description": "d",
        "anyOf": [{"allOf": [{"$ref": "#/components/schemas/A"}]}, {"type": "null"}],
    }
    assert s["C"] == {"type": "number", "exclusiveMinimum": 0, "maximum": 5}
    assert s["D"]["properties"]["nullable"] == {"type": "string"}


def test_unsupported_version() -> None:
    with pytest.raises(NormalizeError):
        normalize({"openapi": "4.0.0"}, "x")


def test_external_refs_are_bundled(specs: pathlib.Path) -> None:
    api = ir_for(specs / "external" / "main.yaml")
    assert set(api.schemas) == {"Book", "Author"}
    book = api.schemas["Book"]
    assert book.property("author").schema.ref == "Author"  # type: ignore[union-attr]
    assert book.property("related").schema.items.ref == "Book"  # type: ignore[union-attr]
    list_books = next(o for o in api.operations if o.operation_id == "listBooks")
    assert list_books.parameters[0].name == "limit"


def test_nullable_30_spec(specs: pathlib.Path) -> None:
    api = ir_for(specs / "nullable_30.yaml")
    note = api.schemas["Note"]
    assert note.property("body").schema.nullable  # type: ignore[union-attr]
    assert note.property("archived_at").schema.format == "date-time"  # type: ignore[union-attr]
    color = note.property("color").schema  # type: ignore[union-attr]
    assert (
        color.kind is Kind.ENUM and color.nullable and color.values == ["red", "blue"]
    )
    author = note.property("author").schema  # type: ignore[union-attr]
    assert author.kind is Kind.REF and author.nullable
    assert api.servers[0].url == "https://eu.notes.test/api"


def test_ir_union_inheritance_and_streams(specs: pathlib.Path) -> None:
    api = ir_for(specs / "acme.yaml")
    pm = api.schemas["PaymentMethod"]
    assert pm.kind is Kind.UNION and pm.discriminator is not None
    assert pm.discriminator.mapping == {"card": "Card", "bank_account": "BankAccount"}
    assert api.schemas["Card"].bases == ["PaymentMethodBase"]
    completion = next(o for o in api.operations if o.operation_id == "createCompletion")
    assert completion.responses[0].item_schema is not None
    assert api.security_schemes["bearerAuth"].scheme == "bearer"


def test_ir_snapshot(specs: pathlib.Path) -> None:
    """The acme IR is checked in; any change in how specs are read shows up here."""
    actual = (
        json.dumps(to_jsonable(ir_for(specs / "acme.yaml")), indent=1, sort_keys=True)
        + "\n"
    )
    golden = GOLDEN / "acme.ir.json"
    if os.environ.get("APISMITH_UPDATE_GOLDEN") or not golden.exists():
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(actual)
    assert actual == golden.read_text()


def test_schema_edge_cases() -> None:
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "E", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "Tuple": {
                    "type": "array",
                    "prefixItems": [{"type": "string"}, {"type": "integer"}],
                },
                "Multi": {"type": ["string", "integer", "null"]},
                "Pattern": {
                    "type": "object",
                    "patternProperties": {"^x-": {"type": "string"}},
                },
                "Const": {"const": "fixed"},
                "Empty": {},
                "OnlyNull": {"type": "null"},
                "SiblingRef": {
                    "$ref": "#/components/schemas/Const",
                    "description": "doc",
                },
                "RefWithProps": {
                    "$ref": "#/components/schemas/Base",
                    "properties": {"x": {"type": "integer"}},
                },
                "Base": {"type": "object", "properties": {"id": {"type": "string"}}},
                "Binary": {"type": "string", "contentMediaType": "image/png"},
                "Deep": {"$ref": "#/components/schemas/Base/properties/id"},
            }
        },
    }
    api = build_ir(normalize(doc, "x"))
    s = api.schemas
    assert s["Tuple"].kind is Kind.ARRAY and s["Tuple"].lossy
    assert s["Multi"].kind is Kind.UNION and s["Multi"].nullable
    assert s["Pattern"].kind is Kind.MAP and s["Pattern"].lossy
    assert s["Const"].kind is Kind.CONST
    assert s["Empty"].kind is Kind.ANY
    assert s["OnlyNull"].kind is Kind.NULL
    assert s["SiblingRef"].kind is Kind.REF and s["SiblingRef"].description == "doc"
    assert s["RefWithProps"].bases == ["Base"]
    assert s["Binary"].format == "binary"
    assert s["Deep"].kind is Kind.REF
    assert s[s["Deep"].ref].kind is Kind.STRING  # type: ignore[index]

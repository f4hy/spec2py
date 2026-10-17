import pathlib
from typing import Any

import pytest

from spec2py.build_ir import build_ir
from spec2py.config import Config, OperationConfig, PaginationConfig
from spec2py.naming import (
    NameRegistry,
    camel_words,
    pascal,
    safe_identifier,
    singular,
    snake,
)
from spec2py.normalize import normalize
from spec2py.pipeline import load_api
from spec2py.plan import PAlias, Plan, PMethod, PModel, plan_api


def plan_of(doc: dict[str, Any], **config: Any) -> Plan:
    base = {"openapi": "3.1.0", "info": {"title": "T", "version": "1"}, "paths": {}}
    return plan_api(build_ir(normalize({**base, **doc}, "x")), Config(**config))


def method(plan: Plan, resource: str, name: str) -> PMethod:
    r = next(r for r in plan.resources if r.attr == resource)
    return next(m for m in r.methods if m.name == name)


def model(plan: Plan, name: str) -> PModel:
    decl = next(m for m in plan.models if m.name == name)
    assert isinstance(decl, PModel)
    return decl


# -- naming ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("listHTTPInvoices_v2", ["list", "http", "invoices", "v2"]),
        ("customer-id", ["customer", "id"]),
        ("ÜberName", ["uber", "name"]),
        ("XMLParser", ["xml", "parser"]),
    ],
)
def test_camel_words(text: str, words: list[str]) -> None:
    assert camel_words(text) == words


def test_naming_helpers() -> None:
    assert snake("getInvoiceById") == "get_invoice_by_id"
    assert pascal("line_items") == "LineItems"
    assert safe_identifier("class") == "class_"
    assert safe_identifier("2fa") == "n_2fa"
    assert safe_identifier("x-rate") == "x_rate"
    assert [singular(w) for w in ("categories", "boxes", "status", "items", "bus")] == [
        "category",
        "box",
        "status",
        "item",
        "bus",
    ]
    reg = NameRegistry({"taken"})
    assert reg.claim("a") == "a"
    assert reg.claim("a") == "a2"
    assert reg.claim("taken", suffix="Model") == "takenModel"


@pytest.mark.parametrize(
    ("title", "client", "package"),
    [
        ("Acme API", "Acme", "acme"),
        ("GitHub v3 REST API", "GitHub", "github"),
        ("Swagger Petstore - OpenAPI 3.0", "SwaggerPetstore", "swagger_petstore"),
        ("my-service", "MyService", "my_service"),
    ],
)
def test_client_naming(title: str, client: str, package: str) -> None:
    plan = plan_of({"info": {"title": title, "version": "1"}})
    assert (plan.client.class_name, plan.client.package) == (client, package)
    assert plan.client.env_prefix == package.upper()


def test_client_naming_from_config() -> None:
    plan = plan_of({}, client_name="Widgets", package="widgets_client", env_prefix="W")
    assert (plan.client.class_name, plan.client.package, plan.client.env_prefix) == (
        "Widgets",
        "widgets_client",
        "W",
    )


# -- methods -----------------------------------------------------------------------


def op(op_id: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tags": ["invoices"],
        "responses": {"200": {"description": "ok"}},
        **extra,
    }
    if op_id:
        body["operationId"] = op_id
    return body


def test_method_names_strip_resource_words() -> None:
    plan = plan_of(
        {
            "paths": {
                "/invoices": {"get": op("listInvoices"), "post": op("createInvoice")},
                "/invoices/{id}": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "get": op("getInvoiceById"),
                    "delete": op("invoices_delete"),
                },
                "/invoices/{id}/send": {
                    "post": op(
                        None,
                        parameters=[{"name": "id", "in": "path", "required": True}],
                    )
                },
                "/invoices/search": {"get": op(None)},
            }
        }
    )
    names = [m.name for m in plan.resources[0].methods]
    assert names == ["list", "create", "retrieve", "delete", "send", "list_search"]
    assert any("no operationId" in n for n in plan.notes)


def test_method_name_collisions_fall_back_to_full_names() -> None:
    plan = plan_of(
        {
            "paths": {
                "/invoices": {"get": op("listInvoices")},
                "/invoices/all": {"get": op("listInvoicesAll", **{"x-unused": 1})},
                "/invoices/x": {"get": op("list_invoices")},
            }
        }
    )
    names = [m.name for m in plan.resources[0].methods]
    assert names[0] == "list"
    assert len(set(names)) == 3


def test_fastapi_default_operation_ids_use_the_function_name() -> None:
    plan = plan_of(
        {
            "paths": {
                "/api/match/{match_id}": {
                    "get": op("get_match_by_id_api_match__match_id__get", tags=[]),
                    "delete": op("reset_match_api_match__match_id__delete", tags=[]),
                },
                "/api/matches/by_date/{date}": {
                    "get": op(
                        "get_matches_by_date_api_matches_by_date__date__get", tags=[]
                    )
                },
                # Not FastAPI's shape: the suffix alone is kept as written.
                "/api/dates/": {"get": op("_api_dates__get", tags=[])},
            }
        }
    )
    names = {r.attr: [m.name for m in r.methods] for r in plan.resources}
    assert names["match"] == ["retrieve", "reset"]
    assert names["matches"] == ["get_by_date"]
    assert names["dates"] == ["api_get"]


def test_config_renames_regroups_and_excludes() -> None:
    plan = plan_of(
        {
            "paths": {
                "/invoices": {"get": op("listInvoices"), "post": op("createInvoice")},
                "/internal/debug": {"get": op("debug", tags=["internal"])},
            }
        },
        operations={
            "GET /invoices": OperationConfig(name="all", resource="billing"),
            "createInvoice": OperationConfig(exclude=True),
        },
        exclude_paths=("/internal/*",),
    )
    assert [(r.attr, [m.name for m in r.methods]) for r in plan.resources] == [
        ("billing", ["all"])
    ]


def test_group_by_path_skips_version_segments() -> None:
    plan = plan_of(
        {
            "paths": {
                "/v2/users/{id}": {
                    "get": op(
                        "getUser",
                        parameters=[{"name": "id", "in": "path", "required": True}],
                    )
                }
            }
        },
        group_by="path",
    )
    assert plan.resources[0].attr == "users"


def test_parameters_become_keyword_arguments() -> None:
    plan = plan_of(
        {
            "paths": {
                "/invoices/{invoice-id}": {
                    "post": op(
                        "updateInvoice",
                        parameters=[
                            {
                                "name": "invoice-id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "dryRun",
                                "in": "query",
                                "schema": {"type": "boolean"},
                            },
                            {
                                "name": "X-Trace",
                                "in": "header",
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "timeout",
                                "in": "query",
                                "schema": {"type": "integer"},
                            },
                            {
                                "name": "ids",
                                "in": "query",
                                "style": "pipeDelimited",
                                "schema": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                },
                            },
                        ],
                        requestBody={
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["dryRun"],
                                        "properties": {
                                            "dryRun": {"type": "string"},
                                            "id": {"type": "string", "readOnly": True},
                                        },
                                    }
                                }
                            }
                        },
                    )
                }
            }
        }
    )
    m = method(plan, "invoices", "update")
    params = {p.py_name: (p.location, p.wire_name) for p in m.params}
    assert params == {
        "invoice_id": ("path", "invoice-id"),
        "dry_run": ("query", "dryRun"),
        "x_trace": ("header", "X-Trace"),
        "timeout_query": ("query", "timeout"),
        "ids": ("query", "ids"),
        "dry_run_body": ("body", "dry_run"),
    }
    assert m.path == "/invoices/{invoice_id}"
    assert m.query_styles == {"ids": ("pipeDelimited", False)}
    assert m.body_mode == "flat"


def test_non_object_bodies_and_content_types() -> None:
    plan = plan_of(
        {
            "paths": {
                "/a": {
                    "post": op(
                        "bulk",
                        requestBody={
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                }
                            }
                        },
                    )
                },
                "/b": {
                    "put": op(
                        "upload",
                        requestBody={
                            "content": {
                                "image/png": {
                                    "schema": {"type": "string", "format": "binary"}
                                }
                            }
                        },
                    )
                },
                "/c": {
                    "post": op(
                        "note",
                        requestBody={
                            "content": {"text/plain": {"schema": {"type": "string"}}}
                        },
                    )
                },
                "/d": {
                    "post": op(
                        "form",
                        requestBody={
                            "content": {
                                "application/x-www-form-urlencoded": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"q": {"type": "string"}},
                                    }
                                }
                            }
                        },
                    )
                },
            }
        }
    )
    assert method(plan, "invoices", "bulk").body_mode == "whole"
    upload = method(plan, "invoices", "upload")
    assert (upload.body_mode, upload.content_type, upload.params[0].type.render()) == (
        "content",
        "image/png",
        "bytes",
    )
    assert method(plan, "invoices", "note").text_body
    assert method(plan, "invoices", "form").body_mode == "form"


# -- types -------------------------------------------------------------------------


def test_type_mapping_table(specs: pathlib.Path) -> None:
    plan = load_api(str(specs / "acme.yaml")).plan
    inv = {f.py_name: f for f in model(plan, "Invoice").fields}
    assert inv["status"].type.render() == "InvoiceStatus"
    assert inv["customer_id"].alias == "customerId"
    assert inv["created_at"].type.render() == "datetime"
    assert inv["memo"].type.render() == "str | None"
    assert inv["collection_method"].type.render() == "InvoiceCollectionMethod | None"
    aliases = {m.name: m.type.render() for m in plan.models if isinstance(m, PAlias)}
    assert (
        aliases["InvoiceStatus"]
        == "Annotated[Literal['draft', 'open', 'paid', 'void'], Tolerant]"
    )
    assert aliases["Metadata"] == "dict[str, str]"
    assert (
        aliases["PaymentMethod"]
        == "Annotated[Card | BankAccount, Field(discriminator='type')]"
    )
    customer = {f.py_name: f for f in model(plan, "Customer").fields}
    assert customer["class_"].alias == "class"
    assert customer["id"].type.render() == "UUID"
    assert {f.py_name: f.type.render() for f in model(plan, "SearchFilter").fields}[
        "value"
    ] == "str | float | bool"
    assert model(plan, "Category").fields[2].type.render() == "list[Category] | None"


def test_discriminated_variants_copy_base_fields_instead_of_narrowing(
    specs: pathlib.Path,
) -> None:
    plan = load_api(str(specs / "acme.yaml")).plan
    card = model(plan, "Card")
    assert card.bases == []
    fields = {f.py_name: f for f in card.fields}
    assert fields["type"].type.render() == "Literal['card']"
    assert fields["type"].default == "card"
    assert {"id", "created_at", "last4"} <= set(fields)


def test_inheritance_is_kept_when_safe() -> None:
    plan = plan_of(
        {
            "components": {
                "schemas": {
                    "Base": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                    "Child": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Base"},
                            {
                                "properties": {
                                    "extra": {"type": "integer"},
                                    "id": {"description": "same type"},
                                }
                            },
                        ]
                    },
                    "Narrow": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Base"},
                            {"properties": {"id": {"type": "integer"}}},
                        ]
                    },
                    "Two": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Base"},
                            {"$ref": "#/components/schemas/Other"},
                        ]
                    },
                    "Other": {
                        "type": "object",
                        "properties": {"o": {"type": "string"}},
                    },
                }
            }
        }
    )
    assert model(plan, "Child").bases == ["Base"]
    assert [f.py_name for f in model(plan, "Child").fields] == ["extra"]
    assert model(plan, "Narrow").bases == []  # a type change can't be an override
    two = model(plan, "Two")
    assert two.bases == ["Base"] and [f.py_name for f in two.fields] == ["o"]
    order = [m.name for m in plan.models]
    assert order.index("Base") < order.index("Child")


def test_inline_schemas_get_one_model_each() -> None:
    plan = plan_of(
        {
            "paths": {
                "/invoices": {
                    "post": op(
                        "createInvoice",
                        requestBody={
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "lines": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "sku": {"type": "string"}
                                                    },
                                                },
                                            },
                                            "mode": {
                                                "type": "string",
                                                "enum": ["a", "b"],
                                            },
                                        },
                                    }
                                }
                            }
                        },
                    )
                }
            }
        }
    )
    names = sorted(m.name for m in plan.models)
    assert names == [
        "InvoiceCreateParams",
        "InvoiceCreateParamsLine",
        "InvoiceCreateParamsMode",
    ]
    create = method(plan, "invoices", "create")
    assert {p.py_name: p.type.render() for p in create.params} == {
        "lines": "list[InvoiceCreateParamsLine]",
        "mode": "Literal['a', 'b']",
    }


def test_keyword_and_reserved_field_names() -> None:
    plan = plan_of(
        {
            "components": {
                "schemas": {
                    "M": {
                        "type": "object",
                        "properties": {
                            "from": {"type": "string"},
                            "model_config": {"type": "string"},
                            "_private": {"type": "string"},
                            "json": {"type": "string"},
                        },
                    }
                }
            }
        }
    )
    assert [f.py_name for f in model(plan, "M").fields] == [
        "from_",
        "model_config_",
        "private",
        "json_",
    ]


def test_reserved_model_names_are_renamed() -> None:
    plan = plan_of(
        {
            "components": {
                "schemas": {
                    "Field": {
                        "type": "object",
                        "properties": {"a": {"type": "string"}},
                    },
                    "BaseModel": {"type": "string"},
                }
            }
        }
    )
    assert {m.name for m in plan.models} == {"FieldModel", "BaseModelModel"}


# -- pagination and streaming ----------------------------------------------------------


def page_op(
    params: list[str],
    props: dict[str, Any],
    headers: dict[str, Any] | None = None,
    array: bool = False,
) -> dict[str, Any]:
    item = {"type": "object", "properties": {"id": {"type": "string"}}}
    schema = (
        {"type": "array", "items": item}
        if array
        else {"type": "object", "properties": {**props}}
    )
    response: dict[str, Any] = {
        "description": "ok",
        "content": {"application/json": {"schema": schema}},
    }
    if headers:
        response["headers"] = headers
    return {
        "get": op(
            "listInvoices",
            parameters=[
                {"name": p, "in": "query", "schema": {"type": "string"}} for p in params
            ],
            responses={"200": response},
        )
    }


ITEMS = {
    "data": {
        "type": "array",
        "items": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
}


@pytest.mark.parametrize(
    ("params", "props", "headers", "array", "strategy", "options"),
    [
        (
            ["cursor"],
            {**ITEMS, "next_cursor": {"type": "string"}},
            None,
            False,
            "CursorPagination",
            {"cursor_param": "cursor", "next_cursor_path": "next_cursor"},
        ),
        (
            ["page_token"],
            {
                **ITEMS,
                "meta": {
                    "type": "object",
                    "properties": {"nextPageToken": {"type": "string"}},
                },
            },
            None,
            False,
            "CursorPagination",
            {"next_cursor_path": "meta.nextPageToken"},
        ),
        (
            ["starting_after", "limit"],
            {**ITEMS, "has_more": {"type": "boolean"}},
            None,
            False,
            "CursorPagination",
            {"cursor_from_item": "id", "has_more": "has_more"},
        ),
        (
            ["offset", "limit"],
            {**ITEMS, "total": {"type": "integer"}},
            None,
            False,
            "OffsetPagination",
            {"offset_param": "offset", "limit_param": "limit", "total": "total"},
        ),
        (
            ["page", "per_page"],
            {"results": ITEMS["data"], "total_pages": {"type": "integer"}},
            None,
            False,
            "PageNumberPagination",
            {"items": "results", "total_pages": "total_pages"},
        ),
        (
            ["page"],
            {},
            {"Link": {"schema": {"type": "string"}}},
            True,
            "LinkHeaderPagination",
            {"items": None},
        ),
        (["page"], {}, None, True, "PageNumberPagination", {"items": None}),
    ],
)
def test_pagination_detection(
    params: list[str],
    props: dict[str, Any],
    headers: dict[str, Any] | None,
    array: bool,
    strategy: str,
    options: dict[str, Any],
) -> None:
    plan = plan_of({"paths": {"/invoices": page_op(params, props, headers, array)}})
    m = method(plan, "invoices", "list")
    assert m.pagination is not None
    assert m.pagination.strategy == strategy
    assert options.items() <= m.pagination.options.items()
    assert m.pagination.item_type.render().startswith("Invoice")


def test_no_pagination_without_a_paging_parameter() -> None:
    plan = plan_of({"paths": {"/invoices": page_op(["status"], ITEMS)}})
    assert method(plan, "invoices", "list").pagination is None


def test_pagination_from_config_and_extension() -> None:
    doc = {
        "paths": {
            "/invoices": page_op(["after"], {**ITEMS, "last": {"type": "string"}})
        }
    }
    plan = plan_of(
        doc,
        operations={
            "listInvoices": OperationConfig(
                pagination=PaginationConfig(
                    style="cursor", cursor_param="after", next_cursor="last"
                )
            )
        },
    )
    pg = method(plan, "invoices", "list").pagination
    assert (
        pg is not None
        and pg.reason == "configured"
        and pg.options["next_cursor_path"] == "last"
    )
    doc["paths"]["/invoices"]["get"]["x-pagination"] = {
        "style": "offset",
        "items": "data",
    }
    pg = method(plan_of(doc), "invoices", "list").pagination
    assert pg is not None and pg.strategy == "OffsetPagination"
    plan = plan_of(doc, operations={"listInvoices": OperationConfig(pagination=False)})
    assert method(plan, "invoices", "list").pagination is None


def test_streaming_detection_and_dual_mode() -> None:
    event = {"type": "object", "properties": {"delta": {"type": "string"}}}
    plan = plan_of(
        {
            "paths": {
                "/completions": {
                    "post": op(
                        "createCompletion",
                        requestBody={
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "prompt": {"type": "string"},
                                            "stream": {"type": "boolean"},
                                        },
                                    }
                                }
                            }
                        },
                        responses={
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"text": {"type": "string"}},
                                        }
                                    },
                                    "text/event-stream": {"itemSchema": event},
                                },
                            }
                        },
                    )
                },
                "/logs": {
                    "get": op(
                        "tailLogs",
                        responses={
                            "200": {
                                "description": "ok",
                                "content": {"application/x-ndjson": {"schema": event}},
                            }
                        },
                    )
                },
                "/chat": {
                    "post": op(
                        "createChat",
                        responses={
                            "200": {
                                "description": "ok",
                                "content": {
                                    "text/event-stream": {"schema": {"type": "string"}}
                                },
                            }
                        },
                    )
                },
            }
        }
    )
    create = method(plan, "invoices", "create_completion")
    stream = method(plan, "invoices", "create_completion_stream")
    assert create.kind == "plain" and "stream" not in {
        p.wire_name for p in create.params
    }
    assert stream.kind == "stream" and stream.fixed_body == {"stream": True}
    assert stream.stream is not None and stream.stream.format == "sse"
    logs = method(plan, "invoices", "tail_logs")
    assert logs.stream is not None and logs.stream.format == "ndjson"
    chat = method(plan, "invoices", "create_chat")
    assert chat.kind == "stream"


def test_errors_and_return_types() -> None:
    err = {"type": "object", "properties": {"message": {"type": "string"}}}
    plan = plan_of(
        {
            "components": {"schemas": {"Err": err}},
            "paths": {
                "/invoices/{id}": {
                    "get": op(
                        "getInvoice",
                        parameters=[{"name": "id", "in": "path", "required": True}],
                        responses={
                            "200": {
                                "description": "ok",
                                "content": {"application/pdf": {}},
                            },
                            "404": {
                                "description": "nf",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Err"}
                                    }
                                },
                            },
                            "5XX": {
                                "description": "x",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Err"}
                                    }
                                },
                            },
                            "default": {
                                "description": "x",
                                "content": {"application/json": {"schema": err}},
                            },
                        },
                    )
                }
            },
        }
    )
    m = method(plan, "invoices", "retrieve")
    assert m.return_type.render() == "bytes"
    assert [(k, t.render()) for k, t in m.errors] == [
        (404, "Err"),
        ("5XX", "Err"),
        ("default", "InvoiceRetrieveError"),
    ]


def test_auth_planning() -> None:
    plan = plan_of(
        {
            "components": {
                "securitySchemes": {
                    "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
                    "basic": {"type": "http", "scheme": "basic"},
                    "cc": {
                        "type": "oauth2",
                        "flows": {
                            "clientCredentials": {
                                "tokenUrl": "/oauth/token",
                                "scopes": {"read": "r"},
                            }
                        },
                    },
                    "digest": {"type": "http", "scheme": "digest"},
                }
            },
            "security": [{"key": []}, {"basic": []}, {"cc": []}, {"digest": []}],
        }
    )
    auth = {a.scheme: (a.kind, [p for p, _ in a.params]) for a in plan.client.auth}
    assert auth == {
        "key": ("api_key", ["api_key"]),
        "basic": ("basic", ["username", "password"]),
        "cc": ("oauth2", ["client_id", "client_secret"]),
    }
    assert any("digest" in n for n in plan.notes)


def test_idempotency_header_detected() -> None:
    plan = plan_of(
        {
            "paths": {
                "/invoices": {
                    "post": op(
                        "createInvoice",
                        parameters=[
                            {
                                "name": "Idempotency-Key",
                                "in": "header",
                                "schema": {"type": "string"},
                            }
                        ],
                    )
                }
            }
        }
    )
    assert plan.client.idempotency_header == "Idempotency-Key"


def test_relative_server_requires_base_url() -> None:
    plan = plan_of({"servers": [{"url": "/api"}]})
    assert plan.client.base_url is None
    assert any("relative" in n for n in plan.notes)

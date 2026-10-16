"""Up-convert Swagger 2.0 documents to OpenAPI 3.0."""

import copy
from typing import Any, cast

__all__ = ["swagger2_to_openapi3"]

type JSON = Any

_SCHEMA_KEYS = (
    "type",
    "format",
    "items",
    "enum",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "multipleOf",
)
_COLLECTION_STYLE = {
    "csv": ("form", False),
    "ssv": ("spaceDelimited", False),
    "pipes": ("pipeDelimited", False),
    "tsv": ("form", False),
    "multi": ("form", True),
}
_FLOW_NAMES = {
    "implicit": "implicit",
    "password": "password",
    "application": "clientCredentials",
    "accessCode": "authorizationCode",
}


def _rewrite_refs(node: JSON, body_params: set[str]) -> JSON:
    if isinstance(node, dict):
        out: dict[str, JSON] = {}
        for key, value in cast(dict[str, JSON], node).items():
            if key == "$ref" and isinstance(value, str):
                out[key] = _rewrite_ref(value, body_params)
            elif key == "x-nullable":
                out["nullable"] = value
            else:
                out[key] = _rewrite_refs(value, body_params)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(v, body_params) for v in cast(list[JSON], node)]
    return node


def _rewrite_ref(ref: str, body_params: set[str]) -> str:
    doc, _, pointer = ref.partition("#")
    for old, new in (
        ("/definitions/", "/components/schemas/"),
        ("/responses/", "/components/responses/"),
    ):
        if pointer.startswith(old):
            return f"{doc}#{new}{pointer.removeprefix(old)}"
    if pointer.startswith("/parameters/"):
        name = pointer.removeprefix("/parameters/")
        section = "requestBodies" if name in body_params else "parameters"
        return f"{doc}#/components/{section}/{name}"
    return ref


def _file_to_binary(schema: JSON) -> JSON:
    if isinstance(schema, dict):
        s = cast(dict[str, JSON], schema)
        if s.get("type") == "file":
            return {
                **{k: v for k, v in s.items() if k != "type"},
                "type": "string",
                "format": "binary",
            }
        return {k: _file_to_binary(v) for k, v in s.items()}
    if isinstance(schema, list):
        return [_file_to_binary(v) for v in cast(list[JSON], schema)]
    return schema


def _param_schema(param: dict[str, JSON]) -> dict[str, JSON]:
    return cast(
        dict[str, JSON],
        _file_to_binary({k: param[k] for k in _SCHEMA_KEYS if k in param}),
    )


def _convert_param(param: dict[str, JSON]) -> dict[str, JSON]:
    out = {
        k: v
        for k, v in param.items()
        if k not in (*_SCHEMA_KEYS, "collectionFormat", "allowEmptyValue")
    }
    if "$ref" in param:
        return param
    out["schema"] = _param_schema(param)
    if (fmt := param.get("collectionFormat")) and param.get("type") == "array":
        style, explode = _COLLECTION_STYLE.get(fmt, ("form", False))
        if param.get("in") == "query":
            out["style"] = style
            out["explode"] = explode
    return out


def _form_body(params: list[dict[str, JSON]], consumes: list[str]) -> dict[str, JSON]:
    properties: dict[str, JSON] = {}
    required: list[str] = []
    has_file = False
    for p in params:
        schema = _param_schema(p)
        if p.get("description"):
            schema["description"] = p["description"]
        has_file |= p.get("type") == "file"
        properties[p["name"]] = schema
        if p.get("required"):
            required.append(p["name"])
    media = (
        "multipart/form-data"
        if has_file or "multipart/form-data" in consumes
        else "application/x-www-form-urlencoded"
    )
    body: dict[str, JSON] = {"type": "object", "properties": properties}
    if required:
        body["required"] = required
    return {"content": {media: {"schema": body}}, "required": bool(required)}


def _convert_responses(
    responses: dict[str, JSON], produces: list[str]
) -> dict[str, JSON]:
    out: dict[str, JSON] = {}
    for status, response in responses.items():
        if not isinstance(response, dict):
            continue
        r = cast(dict[str, JSON], response)
        if "$ref" in r:
            out[status] = r
            continue
        new: dict[str, JSON] = {"description": r.get("description", "")}
        if "schema" in r:
            schema = _file_to_binary(r["schema"])
            new["content"] = {media: {"schema": schema} for media in produces}
            if "examples" in r and isinstance(r["examples"], dict):
                for media, example in cast(dict[str, JSON], r["examples"]).items():
                    if media in new["content"]:
                        new["content"][media]["example"] = example
        if "headers" in r:
            new["headers"] = {
                name: {
                    "description": h.get("description", ""),
                    "schema": _param_schema(h),
                }
                for name, h in cast(dict[str, dict[str, JSON]], r["headers"]).items()
            }
        out[status] = new
    return out


def _security_scheme(scheme: dict[str, JSON]) -> dict[str, JSON]:
    match scheme.get("type"):
        case "basic":
            return {
                "type": "http",
                "scheme": "basic",
                "description": scheme.get("description", ""),
            }
        case "apiKey":
            return scheme
        case "oauth2":
            flow = _FLOW_NAMES.get(cast(str, scheme.get("flow")), "clientCredentials")
            body: dict[str, JSON] = {"scopes": scheme.get("scopes", {})}
            if "authorizationUrl" in scheme:
                body["authorizationUrl"] = scheme["authorizationUrl"]
            if "tokenUrl" in scheme:
                body["tokenUrl"] = scheme["tokenUrl"]
            return {"type": "oauth2", "flows": {flow: body}}
        case _:
            return scheme


def swagger2_to_openapi3(document: dict[str, JSON]) -> dict[str, JSON]:
    """Return an OpenAPI 3.0 equivalent of a Swagger 2.0 document."""
    src = copy.deepcopy(document)
    global_params = cast(dict[str, dict[str, JSON]], src.get("parameters", {}))
    body_params = {name for name, p in global_params.items() if p.get("in") == "body"}
    src = cast(dict[str, JSON], _rewrite_refs(src, body_params))
    global_params = cast(dict[str, dict[str, JSON]], src.get("parameters", {}))
    consumes_default = cast(list[str], src.get("consumes") or ["application/json"])
    produces_default = cast(list[str], src.get("produces") or ["application/json"])

    out: dict[str, JSON] = {"openapi": "3.0.3", "info": src.get("info", {})}
    if host := src.get("host"):
        schemes = cast(list[str], src.get("schemes") or ["https"])
        base_path = src.get("basePath", "")
        out["servers"] = [
            {"url": f"{scheme}://{host}{base_path}"} for scheme in schemes
        ]
    elif base_path := src.get("basePath"):
        out["servers"] = [{"url": base_path}]
    for key in ("tags", "security", "externalDocs"):
        if key in src:
            out[key] = src[key]
    out.update({k: v for k, v in src.items() if k.startswith("x-")})

    components: dict[str, JSON] = {}
    if defs := src.get("definitions"):
        components["schemas"] = _file_to_binary(defs)
    params = {
        n: _convert_param(p)
        for n, p in global_params.items()
        if p.get("in") not in ("body", "formData")
    }
    if params:
        components["parameters"] = params
    request_bodies: dict[str, JSON] = {}
    for name, p in global_params.items():
        if p.get("in") == "body":
            request_bodies[name] = {
                "content": {
                    media: {"schema": p.get("schema", {})} for media in consumes_default
                },
                "required": bool(p.get("required")),
                "description": p.get("description", ""),
            }
    if request_bodies:
        components["requestBodies"] = request_bodies
    if responses := src.get("responses"):
        components["responses"] = _convert_responses(responses, produces_default)
    if sec := src.get("securityDefinitions"):
        components["securitySchemes"] = {
            n: _security_scheme(s)
            for n, s in cast(dict[str, dict[str, JSON]], sec).items()
        }
    if components:
        out["components"] = components

    paths: dict[str, JSON] = {}
    for path, raw_item in cast(dict[str, JSON], src.get("paths", {})).items():
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, JSON], raw_item)
        new_item: dict[str, JSON] = {}
        shared = cast(list[dict[str, JSON]], item.get("parameters", []))
        for method, op in item.items():
            if method == "parameters" or not isinstance(op, dict):
                if method.startswith("x-"):
                    new_item[method] = op
                continue
            operation = cast(dict[str, JSON], op)
            consumes = cast(list[str], operation.get("consumes") or consumes_default)
            produces = cast(list[str], operation.get("produces") or produces_default)
            merged: dict[tuple[str, str], dict[str, JSON]] = {}
            for p in [
                *shared,
                *cast(list[dict[str, JSON]], operation.get("parameters", [])),
            ]:
                resolved = p
                if "$ref" in p:
                    ref = cast(str, p["$ref"])
                    name = ref.rsplit("/", 1)[-1]
                    resolved = global_params.get(name, p)
                    if resolved.get("in") not in ("body", "formData"):
                        merged[(name, "$ref")] = p
                        continue
                merged[
                    (cast(str, resolved.get("name")), cast(str, resolved.get("in")))
                ] = resolved
            new_op = {
                k: v
                for k, v in operation.items()
                if k
                not in ("parameters", "consumes", "produces", "responses", "schemes")
            }
            plain = [
                p for p in merged.values() if p.get("in") not in ("body", "formData")
            ]
            if plain:
                new_op["parameters"] = [_convert_param(p) for p in plain]
            body = next((p for p in merged.values() if p.get("in") == "body"), None)
            form = [p for p in merged.values() if p.get("in") == "formData"]
            if body is not None:
                new_op["requestBody"] = {
                    "content": {
                        media: {"schema": body.get("schema", {})} for media in consumes
                    },
                    "required": bool(body.get("required")),
                }
                if body.get("description"):
                    new_op["requestBody"]["description"] = body["description"]
            elif form:
                new_op["requestBody"] = _form_body(form, consumes)
            new_op["responses"] = _convert_responses(
                cast(dict[str, JSON], operation.get("responses", {})), produces
            )
            new_item[method] = new_op
        paths[path] = new_item
    out["paths"] = paths
    return out

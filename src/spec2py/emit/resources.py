"""Emit one module per resource: the async class, its sync twin, and raw-response views."""

import ast
import builtins
from typing import Any

from ..plan import RUNTIME, Plan, PMethod, PParam, PResource
from .pyast import (
    MODELS,
    Imports,
    TName,
    TNone,
    TSub,
    TUnion,
    TypeExpr,
    arg,
    attr,
    call,
    const,
    doc_expr,
    docstring,
)
from .unasync import RUNTIME_RENAMES, twin_module

__all__ = ["emit_resource", "twin_names"]

_NOT_GIVEN = TName("NotGiven", RUNTIME)
_BUILTIN_NAMES = frozenset(dir(builtins))
_RUNTIME_NAMES = (
    "NOT_GIVEN",
    "NotGiven",
    "Headers",
    "Query",
    "Body",
    "TimeoutTypes",
    "FileTypes",
    "AsyncAPIResource",
    "AsyncPaginator",
    "AsyncStreamManager",
    "build_body",
    "path_template",
    "async_to_raw_response_wrapper",
    "CursorPagination",
    "OffsetPagination",
    "PageNumberPagination",
    "LinkHeaderPagination",
)


def twin_names(name: str) -> dict[str, str]:
    """Async -> sync class names for a client or resource class ``name``."""
    return {
        f"Async{name}": name,
        f"Async{name}WithRawResponse": f"{name}WithRawResponse",
    }


class _ResourceEmitter:
    def __init__(self, plan: Plan, resource: PResource, aliases: set[str]) -> None:
        self.plan = plan
        self.resource = resource
        self.aliases = aliases
        names = twin_names(resource.class_name)
        self.imports = Imports(
            here="resources." + resource.attr,
            reserved=[*names, *names.values()],
        )
        method_names = {m.name for m in resource.methods}
        self.imports.shadowed = method_names & _BUILTIN_NAMES
        self.body_shadowed: set[str] = set()
        self.imports.preassign(RUNTIME, *_RUNTIME_NAMES, *RUNTIME_RENAMES.values())
        self.imports.preassign("typing", "cast")
        self.imports.preassign("types", "NoneType")
        self.imports.preassign("warnings", "deprecated")

    # -- helpers ---------------------------------------------------------------

    def rt(self, name: str) -> ast.expr:
        return ast.Name(self.imports.name(RUNTIME, name))

    def ann(self, t: TypeExpr) -> ast.expr:
        return t.to_ast(self.imports)

    def cast_target(self, t: TypeExpr) -> ast.expr:
        """An expression usable as ``cast_to``/``item_type`` (``type[T]``)."""
        if isinstance(t, TNone):
            return ast.Name(self.imports.name("types", "NoneType"))
        # A model class or a ``list[...]``/``dict[...]`` already is a ``type[T]``;
        # unions, literals and type aliases are not.
        is_class = (
            isinstance(t, TName) and not (t.module == MODELS and t.name in self.aliases)
        ) or (
            isinstance(t, TSub)
            and isinstance(t.base, TName)
            and t.base.name in ("list", "dict")
        )
        # Inside the method body a parameter may shadow builtins (``type``,
        # ``object``), so both forms render those as ``builtins.x``.
        saved = self.imports.shadowed
        self.imports.shadowed = saved | self.body_shadowed
        try:
            if is_class:
                return self.ann(t)
            # ``cast(type[X | Y], X | Y)`` keeps the precise result type, where
            # passing the union bare isn't a ``type[T]`` to checkers.
            type_of = TSub(TName("type"), (t.static(),)).to_ast(self.imports)
            value = self.ann(t)
        finally:
            self.imports.shadowed = saved
        return call(ast.Name(self.imports.name("typing", "cast")), type_of, value)

    # -- signature -------------------------------------------------------------

    def param_annotation(self, p: PParam) -> tuple[ast.expr, ast.expr | None]:
        t = p.type.static()
        if p.required:
            return self.ann(t), None
        return self.ann(TUnion.of(t, _NOT_GIVEN)), self.rt("NOT_GIVEN")

    def arguments(self, method: PMethod) -> ast.arguments:
        posonly = [
            arg(p.py_name, self.ann(p.type.static()))
            for p in method.params
            if p.location == "path"
        ]
        kwonly: list[ast.arg] = []
        defaults: list[ast.expr | None] = []
        others = [p for p in method.params if p.location != "path"]
        others.sort(key=lambda p: not p.required)
        for p in others:
            annotation, default = self.param_annotation(p)
            kwonly.append(arg(p.py_name, annotation))
            defaults.append(default)
        for name, extra_type, extra_default in (
            (
                "extra_headers",
                TUnion.of(TName("Headers", RUNTIME), TNone()),
                const(None),
            ),
            ("extra_query", TUnion.of(TName("Query", RUNTIME), TNone()), const(None)),
            ("extra_body", TUnion.of(TName("Body", RUNTIME), TNone()), const(None)),
            (
                "timeout",
                TUnion.of(TName("TimeoutTypes", RUNTIME), _NOT_GIVEN),
                self.rt("NOT_GIVEN"),
            ),
        ):
            kwonly.append(arg(name, self.ann(extra_type)))
            defaults.append(extra_default)
        return ast.arguments(
            posonlyargs=[arg("self"), *posonly] if posonly else [],
            args=[] if posonly else [arg("self")],
            vararg=None,
            kwonlyargs=kwonly,
            kw_defaults=defaults,
            kwarg=None,
            defaults=[],
        )

    def method_doc(self, method: PMethod) -> str | None:
        documented = [p for p in method.params if p.doc]
        args_section = None
        if documented:
            lines = ["Args:"]
            for p in documented:
                text = " ".join((p.doc or "").split())
                lines.append(f"    {p.py_name}: {text}")
            args_section = "\n".join(lines)
        extra = None
        if method.kind == "paginated":
            extra = "Iterate the result to walk every item across all pages."
        elif method.kind == "stream":
            extra = "Use as a context manager; iterating it yields typed events."
        summary = (method.summary or "").strip() or None
        description = (
            method.description if method.description != method.summary else None
        )
        if (
            summary is None
            and description
            and "\n" not in description.strip()
            and len(description) < 80
        ):
            summary, description = description.strip(), None
        if summary is None:
            summary = f"``{method.operation.key}``"
        if summary[-1] not in ".!?`":
            summary += "."
        return docstring(summary, description, extra, args_section, indent=8)

    # -- body ------------------------------------------------------------------

    def mapping(self, params: list[PParam]) -> ast.Dict:
        keys: list[ast.expr | None] = [const(p.wire_name) for p in params]
        return ast.Dict(keys, [ast.Name(p.py_name) for p in params])

    def request_kwargs(self, method: PMethod) -> list[ast.keyword]:
        kw: dict[str, ast.expr] = {}
        by_loc: dict[str, list[PParam]] = {}
        for p in method.params:
            by_loc.setdefault(p.location, []).append(p)
        if query := by_loc.get("query"):
            kw["query"] = self.mapping(query)
        if method.query_styles:
            kw["query_styles"] = ast.Dict(
                [const(k) for k in method.query_styles],
                [
                    ast.Tuple([const(s), const(e)])
                    for s, e in method.query_styles.values()
                ],
            )
        if headers := by_loc.get("header"):
            kw["headers"] = self.mapping(headers)
        if cookies := by_loc.get("cookie"):
            kw["cookies"] = self.mapping(cookies)
        match method.body_mode:
            case "flat":
                assert method.body_model is not None
                fields = self.mapping(by_loc.get("body", []))
                for k, v in method.fixed_body.items():
                    fields.keys.append(const(k))
                    fields.values.append(const(v))
                kw["body"] = call(
                    self.rt("build_body"), self.ann(method.body_model), fields
                )
            case "whole":
                (body,) = by_loc["body"]
                kw["body"] = ast.Name(body.py_name)
            case "multipart" | "form":
                if form := by_loc.get("form"):
                    kw["form"] = self.mapping(form)
                elif method.body_mode == "form":
                    kw["form"] = ast.Dict([], [])
                if files := by_loc.get("file"):
                    kw["files"] = self.mapping(files)
            case "content":
                (content,) = by_loc["content"]
                value: ast.expr = ast.Name(content.py_name)
                if method.text_body:
                    value = call(ast.Attribute(value, "encode"))
                kw["content"] = value
            case "none":
                pass
        ct = method.content_type or (
            method.operation.request_body.media_type
            if method.operation.request_body
            else None
        )
        if (
            ct
            and method.body_mode in ("flat", "whole", "content")
            and ct != "application/json"
        ):
            kw["content_type"] = const(ct)
        if not method.security:
            kw["security"] = const(False)
        if method.errors:
            kw["errors"] = ast.Dict(
                [const(k) for k, _ in method.errors],
                # Error models are looked up at runtime, so any expression works.
                [self.ann(t) for _, t in method.errors],
            )
        for name in ("extra_headers", "extra_query", "extra_body", "timeout"):
            kw[name] = ast.Name(name)
        return [ast.keyword(k, v) for k, v in kw.items()]

    def path_expr(self, method: PMethod) -> ast.expr:
        path_params = [p for p in method.params if p.location == "path"]
        if not path_params:
            return const(method.path)
        return call(
            self.rt("path_template"),
            const(method.path),
            **{p.py_name: ast.Name(p.py_name) for p in path_params},
        )

    # -- methods ---------------------------------------------------------------

    def method(self, method: PMethod) -> ast.stmt:
        self.body_shadowed = {p.py_name for p in method.params} & _BUILTIN_NAMES
        client = attr("self", "_client", "request")
        verb = const(method.http_method)
        path = self.path_expr(method)
        kwargs = self.request_kwargs(method)
        is_async = False
        returns: TypeExpr
        match method.kind:
            case "paginated":
                assert method.pagination is not None
                pg = method.pagination
                strategy = call(
                    self.rt(pg.strategy), **{k: const(v) for k, v in pg.options.items()}
                )
                value: ast.expr = call(
                    attr("self", "_client", "get_api_list"),
                    verb,
                    path,
                    item_type=self.cast_target(pg.item_type),
                    strategy=strategy,
                )
                returns = TSub(TName("AsyncPaginator", RUNTIME), (pg.item_type,))
            case "stream":
                assert method.stream is not None
                st = method.stream
                value = call(
                    attr("self", "_client", "stream"),
                    verb,
                    path,
                    event_type=self.cast_target(st.event_type),
                    stream_format=const(st.format),
                    terminator=const(self.plan.config.stream_terminator),
                )
                returns = TSub(TName("AsyncStreamManager", RUNTIME), (st.event_type,))
            case _:
                value = ast.Await(
                    call(
                        client, verb, path, cast_to=self.cast_target(method.return_type)
                    )
                )
                returns = method.return_type
                is_async = True
        assert isinstance(value, ast.Call | ast.Await)
        target = value.value if isinstance(value, ast.Await) else value
        assert isinstance(target, ast.Call)
        target.keywords.extend(kwargs)
        body: list[ast.stmt] = []
        if text := self.method_doc(method):
            body.append(doc_expr(text))
        body.append(ast.Return(value))
        decorators: list[ast.expr] = []
        if method.deprecated:
            message = " ".join(
                (method.description or "This operation is deprecated.").split()
            )
            decorators.append(
                call(
                    ast.Name(self.imports.name("warnings", "deprecated")),
                    const(message),
                )
            )
        fields: dict[str, Any] = {
            "name": method.name,
            "args": self.arguments(method),
            "body": body,
            "decorator_list": decorators,
            "returns": self.ann(returns.static()),
            "type_params": [],
        }
        return ast.AsyncFunctionDef(**fields) if is_async else ast.FunctionDef(**fields)

    def resource_class(self) -> ast.ClassDef:
        name = f"Async{self.resource.class_name}"
        body: list[ast.stmt] = [
            doc_expr(f"Operations on {self.resource.attr.replace('_', ' ')}.")
        ]
        body.extend(self.method(m) for m in self.resource.methods)
        return ast.ClassDef(
            name=name,
            bases=[self.rt("AsyncAPIResource")],
            keywords=[],
            body=body,
            decorator_list=[],
            type_params=[],
        )

    def raw_class(self) -> ast.ClassDef:
        name = f"Async{self.resource.class_name}WithRawResponse"
        resource_type = f"Async{self.resource.class_name}"
        init_body: list[ast.stmt] = []
        for m in self.resource.methods:
            if m.kind != "plain" or m.deprecated:
                continue
            init_body.append(
                ast.Assign(
                    [ast.Attribute(ast.Name("self"), m.name)],
                    call(
                        self.rt("async_to_raw_response_wrapper"),
                        ast.Attribute(ast.Name("resource"), m.name),
                    ),
                )
            )
        if not init_body:
            init_body.append(ast.Pass())
        init = ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[],
                args=[arg("self"), arg("resource", ast.Name(resource_type))],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=init_body,
            decorator_list=[],
            returns=ast.Constant(None),
            type_params=[],
        )
        doc = doc_expr(
            "Like the resource, but each method returns an ``APIResponse`` with status,\n"
            "    headers and a lazy ``parse()``. Paginated and streaming methods are omitted\n"
            "    (their pages and streams already expose ``.response``), as are deprecated ones.\n    "
        )
        return ast.ClassDef(
            name=name,
            bases=[],
            keywords=[],
            body=[doc, init],
            decorator_list=[],
            type_params=[],
        )

    def module(self) -> ast.Module:
        return twin_module(
            f"The ``{self.resource.attr}`` resource.",
            self.imports,
            twin_names(self.resource.class_name),
            self.resource_class,
            self.raw_class,
        )


def emit_resource(plan: Plan, resource: PResource, aliases: set[str]) -> ast.Module:
    return _ResourceEmitter(plan, resource, aliases).module()

"""Emit ``_client.py``: the client classes, with resources as attributes."""

import ast

from ..plan import RUNTIME, PAuth, Plan, PResource
from .pyast import (
    Imports,
    TName,
    TNone,
    TSub,
    TUnion,
    arg,
    call,
    const,
    doc_expr,
    docstring,
)
from .resources import resource_class_names
from .unasync import RUNTIME_RENAMES, to_sync

__all__ = ["emit_client"]


class _ClientEmitter:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.client = plan.client
        name = self.client.class_name
        self.renames = {
            f"Async{name}": name,
            f"Async{name}WithRawResponse": f"{name}WithRawResponse",
        }
        for r in plan.resources:
            self.renames.update(resource_class_names(r))
        self.custom_locals: dict[str, str] = {}
        self.imports = Imports(
            package=self.client.package,
            here="_client",
            reserved=[*self.renames, *self.renames.values()],
        )
        self.imports.preassign(RUNTIME, *RUNTIME_RENAMES.values(), "AsyncAPIClient")

    def rt(self, name: str) -> ast.expr:
        return ast.Name(self.imports.name(RUNTIME, name))

    def resource_import(self, r: PResource, *, raw: bool = False) -> None:
        module = f".resources.{r.attr}"
        names = resource_class_names(r)
        for async_name, sync_name in names.items():
            if raw == async_name.endswith("WithRawResponse"):
                self.imports.preassign(module, async_name, sync_name)
                self.imports.name(module, async_name)
                self.imports.name(module, sync_name)
        if r.custom and not raw:
            # The user's subclass usually reuses the generated class's name, so
            # it's imported under a clash-safe local name.
            module = "._custom." + r.custom[0].removeprefix("_custom.").removeprefix(
                "."
            )
            async_local = self.imports.name(module, f"Async{r.custom[1]}")
            sync_local = self.imports.name(module, r.custom[1])
            self.renames[async_local] = sync_local
            self.custom_locals[r.attr] = async_local

    def auth_statements(self) -> list[ast.stmt]:
        """Read credentials (argument, then environment) and build ``auth``."""
        out: list[ast.stmt] = []
        for auth in self.client.auth:
            for py, env in auth.params:
                out.append(
                    ast.Assign(
                        [ast.Attribute(ast.Name("self"), py)],
                        ast.IfExp(
                            ast.Compare(ast.Name(py), [ast.IsNot()], [const(None)]),
                            ast.Name(py),
                            call(
                                ast.Attribute(
                                    ast.Attribute(
                                        ast.Name(self.imports.module("os")), "environ"
                                    ),
                                    "get",
                                ),
                                const(env),
                            ),
                        ),
                    )
                )
        if not self.client.auth:
            return out
        auths_ann = ast.Subscript(
            ast.Name("list"),
            ast.Attribute(ast.Name(self.imports.module("httpx2")), "Auth"),
        )
        build: list[ast.stmt] = [
            ast.AnnAssign(ast.Name("auths"), auths_ann, ast.List([]), simple=1)
        ]
        for auth in self.client.auth:
            build.append(self.auth_append(auth))
        build.append(
            ast.Assign(
                [ast.Name("auth")],
                ast.IfExp(
                    ast.Compare(call("len", ast.Name("auths")), [ast.Eq()], [const(1)]),
                    ast.Subscript(ast.Name("auths"), const(0)),
                    ast.IfExp(
                        ast.Name("auths"),
                        call(self.rt("MultiAuth"), ast.Name("auths")),
                        const(None),
                    ),
                ),
            )
        )
        out.append(
            ast.If(ast.Compare(ast.Name("auth"), [ast.Is()], [const(None)]), build, [])
        )
        return out

    def auth_append(self, auth: PAuth) -> ast.stmt:
        attrs = [ast.Attribute(ast.Name("self"), py) for py, _ in auth.params]
        conds: list[ast.expr] = [
            ast.Compare(a, [ast.IsNot()], [const(None)]) for a in attrs
        ]
        test: ast.expr = conds[0] if len(conds) == 1 else ast.BoolOp(ast.And(), conds)
        match auth.kind:
            case "api_key":
                value = call(
                    self.rt("ApiKeyAuth"),
                    attrs[0],
                    name=const(auth.parameter_name or "X-API-Key"),
                    location=const(auth.location or "header"),
                )
            case "bearer":
                value = call(self.rt("BearerAuth"), attrs[0])
            case "basic":
                value = call(self.rt("BasicAuth"), attrs[0], attrs[1])
            case "oauth2":
                token_url = auth.token_url or "/oauth/token"
                token_expr: ast.expr = const(token_url)
                if not token_url.startswith(("http://", "https://")):
                    token_expr = call(
                        "str",
                        call(
                            ast.Attribute(
                                call(
                                    ast.Attribute(
                                        ast.Name(self.imports.module("httpx2")), "URL"
                                    ),
                                    ast.Name("resolved_base_url"),
                                ),
                                "join",
                            ),
                            const(token_url),
                        ),
                    )
                value = call(
                    self.rt("OAuth2ClientCredentials"),
                    token_url=token_expr,
                    client_id=attrs[0],
                    client_secret=attrs[1],
                    scopes=ast.Tuple([const(s) for s in auth.scopes]),
                )
        append = ast.Expr(call(ast.Attribute(ast.Name("auths"), "append"), value))
        return ast.If(test, [append], [])

    def init(self) -> ast.AsyncFunctionDef | ast.FunctionDef:
        c = self.client
        kwonly: list[ast.arg] = []
        defaults: list[ast.expr | None] = []
        str_or_none = TUnion.of(TName("str"), TNone())

        def add(name: str, ann: ast.expr, default: ast.expr) -> None:
            kwonly.append(arg(name, ann))
            defaults.append(default)

        for auth in c.auth:
            for py, _ in auth.params:
                add(py, str_or_none.to_ast(self.imports), const(None))
        httpx = self.imports.module("httpx2")
        add("base_url", str_or_none.to_ast(self.imports), const(None))
        add(
            "auth",
            ast.BinOp(ast.Attribute(ast.Name(httpx), "Auth"), ast.BitOr(), const(None)),
            const(None),
        )
        add(
            "timeout",
            ast.Name(self.imports.name(RUNTIME, "TimeoutTypes")),
            self.rt("DEFAULT_TIMEOUT"),
        )
        add("max_retries", ast.Name("int"), self.rt("DEFAULT_MAX_RETRIES"))

        def mapping(value: str) -> ast.expr:
            mapping_type = TSub(
                TName("Mapping", "collections.abc"), (TName("str"), TName(value))
            )
            return TUnion.of(mapping_type, TNone()).to_ast(self.imports)

        add("default_headers", mapping("str"), const(None))
        add("default_query", mapping("object"), const(None))
        add(
            "hooks",
            TUnion.of(TName("Hooks", RUNTIME), TNone()).to_ast(self.imports),
            const(None),
        )
        add("strict_response_validation", ast.Name("bool"), const(False))
        add(
            "trusted_pagination_origins",
            TSub(TName("Collection", "collections.abc"), (TName("str"),)).to_ast(
                self.imports
            ),
            ast.Tuple([], ast.Load()),
        )
        add(
            "http_client",
            ast.BinOp(
                ast.Attribute(ast.Name(httpx), "AsyncClient"), ast.BitOr(), const(None)
            ),
            const(None),
        )
        add(
            "transport",
            ast.BinOp(
                ast.Attribute(ast.Name(httpx), "AsyncBaseTransport"),
                ast.BitOr(),
                const(None),
            ),
            const(None),
        )

        env_base = f"{c.env_prefix}_BASE_URL"
        environ_get = call(
            ast.Attribute(
                ast.Attribute(ast.Name(self.imports.module("os")), "environ"), "get"
            ),
            const(env_base),
        )
        base_value: ast.expr = ast.BoolOp(ast.Or(), [ast.Name("base_url"), environ_get])
        if c.base_url:
            base_value.values.append(const(c.base_url))  # type: ignore[attr-defined]
        body: list[ast.stmt] = []
        arg_docs = [
            *(
                f"{py}: Defaults to the ``{env}`` environment variable."
                for a in c.auth
                for py, env in a.params
            ),
            f"base_url: Defaults to ``{env_base}``, then ``{c.base_url}``."
            if c.base_url
            else f"base_url: Required; defaults to ``{env_base}``.",
            "auth: A custom ``httpx2.Auth``; overrides the credentials above.",
            "timeout: Seconds (or an ``httpx2.Timeout``) per request.",
            "max_retries: Retries for connection errors, 408/409/429 and 5xx.",
            "hooks: Request/response middleware (logging, signing, test doubles).",
            "trusted_pagination_origins: Other origins (``https://host[:port]``) "
            "that next-page URLs may point at; they receive this client's "
            "credentials.",
            "http_client: Bring your own ``httpx2`` client (proxies, mTLS, pooling).",
            "transport: A custom transport, e.g. ``httpx2.MockTransport`` in tests.",
        ]
        body.append(
            doc_expr(
                docstring(
                    f"Create a client for the {c.api_name}.",
                    "Args:\n" + "\n".join("    " + d for d in arg_docs),
                    indent=8,
                )
                or ""
            )
        )
        body.append(ast.Assign([ast.Name("resolved_base_url")], base_value))
        if not c.base_url:
            body.append(
                ast.If(
                    ast.UnaryOp(ast.Not(), ast.Name("resolved_base_url")),
                    [
                        ast.Raise(
                            call(
                                "ValueError",
                                const(f"base_url is required (or set {env_base})"),
                            ),
                            None,
                        )
                    ],
                    [],
                )
            )
        body.extend(self.auth_statements())
        super_kwargs: dict[str, ast.expr] = {
            "base_url": ast.Name("resolved_base_url"),
            "auth": ast.Name("auth"),
            "timeout": ast.Name("timeout"),
            "max_retries": ast.Name("max_retries"),
            "default_headers": ast.Name("default_headers"),
            "default_query": ast.Name("default_query"),
            "hooks": ast.Name("hooks"),
            "strict_response_validation": ast.Name("strict_response_validation"),
            "trusted_pagination_origins": ast.Name("trusted_pagination_origins"),
            "http_client": ast.Name("http_client"),
            "transport": ast.Name("transport"),
        }
        if c.idempotency_header:
            super_kwargs["idempotency_header"] = const(c.idempotency_header)
        body.append(
            ast.Expr(call(ast.Attribute(call("super"), "__init__"), **super_kwargs))
        )
        return ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[],
                args=[arg("self")],
                vararg=None,
                kwonlyargs=kwonly,
                kw_defaults=defaults,
                kwarg=None,
                defaults=[],
            ),
            body=body,
            decorator_list=[],
            returns=const(None),
            type_params=[],
        )

    def cached(
        self, name: str, returns: ast.expr, value: ast.expr, doc: str | None = None
    ) -> ast.FunctionDef:
        body: list[ast.stmt] = [doc_expr(doc)] if doc else []
        body.append(ast.Return(value))
        return ast.FunctionDef(
            name=name,
            args=ast.arguments(
                posonlyargs=[],
                args=[arg("self")],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=body,
            decorator_list=[
                ast.Name(self.imports.name("functools", "cached_property"))
            ],
            returns=returns,
            type_params=[],
        )

    def client_class(self) -> ast.ClassDef:
        c = self.client
        name = f"Async{c.class_name}"
        body: list[ast.stmt] = []
        doc = docstring(
            f"Client for the {c.api_name} (version {c.version}).", c.description
        )
        if doc:
            body.append(doc_expr(doc))
        body.append(ast.Assign([ast.Name("_package")], const(c.package)))
        body.append(
            ast.Assign(
                [ast.Name("_version")],
                ast.Name(self.imports.name("._version", "__version__")),
            )
        )
        body.append(self.init())
        for r in self.plan.resources:
            self.resource_import(r)
            klass = self.custom_locals.get(r.attr, f"Async{r.class_name}")
            body.append(
                self.cached(r.attr, ast.Name(klass), call(klass, ast.Name("self")))
            )
        raw = f"Async{c.class_name}WithRawResponse"
        body.append(
            self.cached(
                "with_raw_response",
                ast.Name(raw),
                call(raw, ast.Name("self")),
                "Same resources, but methods return ``APIResponse`` (status, headers, ``parse()``).",
            )
        )
        return ast.ClassDef(
            name=name,
            bases=[self.rt("AsyncAPIClient")],
            keywords=[],
            body=body,
            decorator_list=[],
            type_params=[],
        )

    def raw_class(self) -> ast.ClassDef:
        c = self.client
        name = f"Async{c.class_name}WithRawResponse"
        init = ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[],
                args=[arg("self"), arg("client", ast.Name(f"Async{c.class_name}"))],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[
                ast.Assign(
                    [ast.Attribute(ast.Name("self"), "_client")], ast.Name("client")
                )
            ],
            decorator_list=[],
            returns=const(None),
            type_params=[],
        )
        body: list[ast.stmt] = [init]
        for r in self.plan.resources:
            self.resource_import(r, raw=True)
            klass = f"Async{r.class_name}WithRawResponse"
            body.append(
                self.cached(
                    r.attr,
                    ast.Name(klass),
                    call(
                        klass,
                        ast.Attribute(
                            ast.Attribute(ast.Name("self"), "_client"), r.attr
                        ),
                    ),
                )
            )
        return ast.ClassDef(
            name=name,
            bases=[],
            keywords=[],
            body=body,
            decorator_list=[],
            type_params=[],
        )

    def module(self) -> ast.Module:
        async_client = self.client_class()
        async_raw = self.raw_class()
        sync_client = to_sync(async_client, self.renames)
        sync_raw = to_sync(async_raw, self.renames)
        for (module, name), _ in self.imports.used():
            if module == RUNTIME and name in RUNTIME_RENAMES:
                self.imports.name(RUNTIME, RUNTIME_RENAMES[name])
        names = [sync_client.name, async_client.name, sync_raw.name, async_raw.name]
        return ast.Module(
            body=[
                doc_expr(f"Clients for the {self.client.api_name}."),
                *self.imports.statements(),
                ast.Assign([ast.Name("__all__")], ast.List([const(n) for n in names])),
                sync_client,
                async_client,
                sync_raw,
                async_raw,
            ],
            type_ignores=[],
        )


def emit_client(plan: Plan) -> ast.Module:
    return _ClientEmitter(plan).module()

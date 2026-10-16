"""Emit ``models.py``: Pydantic models and type aliases."""

import ast
import builtins

from ..naming import camel_words
from ..plan import MISSING, RUNTIME, PAlias, PField, Plan, PModel
from .pyast import MODELS, Imports, TName, call, const, doc_expr, docstring

__all__ = ["emit_models"]

_BUILTINS = frozenset(dir(builtins))


def _field_value(f: PField, imports: Imports) -> ast.expr | None:
    kwargs: dict[str, ast.expr] = {}
    if f.alias:
        kwargs["alias"] = const(f.alias)
    if f.deprecated:
        kwargs["deprecated"] = const(True)
    if not kwargs:
        return None if f.default is MISSING else const(f.default)
    args: list[ast.expr] = []
    if f.default is not MISSING:
        kwargs = {"default": const(f.default), **kwargs}
    return call(ast.Name(imports.name("pydantic", "Field")), *args, **kwargs)


def _model(model: PModel, imports: Imports) -> ast.ClassDef:
    # Under PEP 649 a field named like a builtin (``object``, ``type``) shadows it
    # in every annotation of the class, so those annotations say ``builtins.x``.
    imports.shadowed = {f.py_name for f in model.fields} & _BUILTINS
    try:
        return _model_body(model, imports)
    finally:
        imports.shadowed = set()


def _model_body(model: PModel, imports: Imports) -> ast.ClassDef:
    body: list[ast.stmt] = []
    extra = None
    if model.extra is not None:
        # A typed ``__pydantic_extra__`` would be an incompatible override for
        # pyright and ty, so the value type is documented instead.
        extra = f"Additional properties are kept in ``model_extra`` (values: ``{model.extra.render()}``)."
    if text := docstring(model.doc, "Deprecated." if model.deprecated else None, extra):
        body.append(doc_expr(text))
    for f in model.fields:
        body.append(
            ast.AnnAssign(
                target=ast.Name(f.py_name),
                annotation=f.type.to_ast(imports),
                value=_field_value(f, imports),
                simple=1,
            )
        )
        if text := docstring(f.doc):
            body.append(doc_expr(text))
    if not body:
        body.append(ast.Pass())
    bases = [TName(b, MODELS).to_ast(imports) for b in model.bases] or [
        ast.Name(imports.name(RUNTIME, "BaseModel"))
    ]
    return ast.ClassDef(
        name=model.name,
        bases=bases,
        keywords=[],
        body=body,
        decorator_list=[],
        type_params=[],
    )


def _alias(alias: PAlias, imports: Imports) -> list[ast.stmt]:
    out: list[ast.stmt] = [
        ast.TypeAlias(
            name=ast.Name(alias.name), type_params=[], value=alias.type.to_ast(imports)
        )
    ]
    if text := docstring(alias.doc, indent=0):
        out.append(doc_expr(text))
    return out


SPLIT_THRESHOLD = 400
"""Specs with more models than this get a ``models/`` package instead of one
module: type checkers (pyright in particular) stop resolving symbols in very
large files."""


def _module(
    plan: Plan, decls: list[PModel | PAlias], imports: Imports
) -> list[ast.stmt]:
    body: list[ast.stmt] = []
    for decl in decls:
        if isinstance(decl, PModel):
            body.append(_model(decl, imports))
        else:
            body.extend(_alias(decl, imports))
    return body


def _field_names(decls: list[PModel | PAlias]) -> set[str]:
    """Field names shadow module names in their class's annotations (PEP 649), so
    an import that collides with one (a field called ``datetime``) gets an alias."""
    return {f.py_name for d in decls if isinstance(d, PModel) for f in d.fields}


def _link(imports: Imports) -> ast.stmt:
    return ast.Expr(
        call(ast.Name(imports.name(RUNTIME, "link_models")), call("globals"))
    )


def emit_models(plan: Plan) -> dict[str, ast.Module]:
    """Build the models module (or package, for large specs), keyed by path."""
    if len(plan.models) > SPLIT_THRESHOLD:
        return _emit_split(plan)
    names = [m.name for m in plan.models]
    imports = Imports(
        package=plan.client.package,
        here=MODELS,
        reserved=[*names, *_field_names(plan.models)],
    )
    body = _module(plan, plan.models, imports)
    header: list[ast.stmt] = [
        doc_expr(f"Models for the {plan.client.api_name}."),
        *imports.statements(),
        ast.Assign([ast.Name("__all__")], ast.List([const(n) for n in sorted(names)])),
    ]
    return {"models.py": ast.Module(body=[*header, *body], type_ignores=[])}


CHUNK_SIZE = 150
"""Most models per module before a group splits on its next name word."""


def _chunk_layout(names: list[str]) -> dict[str, str]:
    """Group model names into modules, deterministically and diff-stably.

    Models start grouped by first letter (``_w``). A group larger than
    ``CHUNK_SIZE`` gives each big name-prefix family its own module
    (``_w_webhook``, then ``_w_webhook_issues`` if still too big); the
    stragglers stay in the parent group. Adding a model only moves models
    within its own family.
    """
    layout: dict[str, str] = {}

    def place(group: list[str], key: str, depth: int) -> None:
        if len(group) <= CHUNK_SIZE:
            layout.update(dict.fromkeys(group, key))
            return
        families: dict[str, list[str]] = {}
        for name in group:
            words = camel_words(name)
            if len(words) >= depth:
                families.setdefault("_".join(words[:depth]), []).append(name)
            else:
                layout[name] = key
        for prefix, members in families.items():
            if (len(members) >= CHUNK_SIZE // 3 and len(families) > 1) or depth == 1:
                place(members, f"{key}_{prefix}" if depth > 1 else key, depth + 1)
            else:
                layout.update(dict.fromkeys(members, key))

    letters: dict[str, list[str]] = {}
    for name in names:
        first = name[0].lower()
        letters.setdefault(f"_{first}" if first.isalpha() else "_other", []).append(
            name
        )
    for key, group in letters.items():
        place(group, key, 1)
    return layout


def _emit_split(plan: Plan) -> dict[str, ast.Module]:
    """Split models across modules without runtime import cycles.

    ``_bases`` holds every class another class inherits from (and its own
    bases), so subclasses import their base eagerly from one place. Type aliases
    live in ``_aliases``, which imports models eagerly because alias values are
    evaluated lazily in that module's namespace. Every other cross-module
    reference is a ``TYPE_CHECKING`` import; ``link_models`` makes those names
    resolvable at runtime, and each model builds lazily on first use.
    """
    by_name = {d.name: d for d in plan.models}
    bases: set[str] = set()
    pending = [b for d in plan.models if isinstance(d, PModel) for b in d.bases]
    while pending:
        name = pending.pop()
        if name not in bases:
            bases.add(name)
            decl = by_name.get(name)
            if isinstance(decl, PModel):
                pending.extend(decl.bases)
    layout = _chunk_layout(
        [d.name for d in plan.models if isinstance(d, PModel) and d.name not in bases]
    )
    layout.update({d.name: "_aliases" for d in plan.models if isinstance(d, PAlias)})
    layout.update({name: "_bases" for name in bases if name in by_name})
    chunks: dict[str, list[PModel | PAlias]] = {}
    for decl in plan.models:
        chunks.setdefault(layout[decl.name], []).append(decl)

    out: dict[str, ast.Module] = {}
    for chunk, decls in sorted(chunks.items()):
        own = [d.name for d in decls]
        imports = Imports(
            package=plan.client.package,
            here=f"models.{chunk}",
            reserved=[*own, *_field_names(decls)],
        )
        imports.model_modules = layout
        body = _module(plan, decls, imports)
        if chunk != "_aliases":
            eager = {b for d in decls if isinstance(d, PModel) for b in d.bases}
            for (module, name), _ in imports.used():
                if module.startswith(".") and name not in eager:
                    imports.deferred.add((module, name))
        out[f"models/{chunk}.py"] = ast.Module(
            body=[
                doc_expr(
                    f"Models for the {plan.client.api_name} ({chunk.lstrip('_')})."
                ),
                *imports.statements(),
                ast.Assign(
                    [ast.Name("__all__")], ast.List([const(n) for n in sorted(own)])
                ),
                *body,
            ],
            type_ignores=[],
        )
    imports = Imports(package=plan.client.package, here="models")
    init: list[ast.stmt] = [doc_expr(f"Models for the {plan.client.api_name}.")]
    for chunk, decls in sorted(chunks.items()):
        init.append(ast.ImportFrom(chunk, [ast.alias(d.name) for d in decls], 1))
    link = _link(imports)
    init[1:1] = imports.statements()
    names = sorted(by_name)
    init.append(ast.Assign([ast.Name("__all__")], ast.List([const(n) for n in names])))
    init.append(link)
    out["models/__init__.py"] = ast.Module(body=init, type_ignores=[])
    return out

"""Emit ``models.py``: Pydantic models and type aliases."""

import ast
from typing import Any

from ..plan import MISSING, RUNTIME, PAlias, PField, PModel, Plan
from .pyast import MODELS, Imports, TName, call, const, doc_expr, docstring

__all__ = ["emit_models"]


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
    body: list[ast.stmt] = []
    if text := docstring(model.doc, "Deprecated." if model.deprecated else None):
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
    if model.extra is not None:
        body.append(
            ast.AnnAssign(
                target=ast.Name("__pydantic_extra__"),
                annotation=ast.Subscript(
                    ast.Name("dict"), ast.Tuple([ast.Name("str"), model.extra.to_ast(imports)])
                ),
                value=call(ast.Name(imports.name("pydantic", "Field")), init=const(False)),
                simple=1,
            )
        )
    if not body:
        body.append(ast.Pass())
    bases = [TName(b, MODELS).to_ast(imports) for b in model.bases] or [
        ast.Name(imports.name(RUNTIME, "BaseModel"))
    ]
    return ast.ClassDef(name=model.name, bases=bases, keywords=[], body=body, decorator_list=[], type_params=[])


def _alias(alias: PAlias, imports: Imports) -> list[ast.stmt]:
    out: list[ast.stmt] = [
        ast.TypeAlias(name=ast.Name(alias.name), type_params=[], value=alias.type.to_ast(imports))
    ]
    if text := docstring(alias.doc, indent=0):
        out.append(doc_expr(text))
    return out


def emit_models(plan: Plan) -> tuple[ast.Module, list[str]]:
    """Build the models module; returns the AST and its exported names."""
    names = [m.name for m in plan.models]
    imports = Imports(package=plan.client.package, here=MODELS, reserved=names)
    body: list[Any] = []
    for decl in plan.models:
        if isinstance(decl, PModel):
            body.append(_model(decl, imports))
        else:
            body.extend(_alias(decl, imports))
    tail: list[ast.stmt] = []
    if any(isinstance(m, PModel) for m in plan.models):
        tail.append(ast.Expr(call(ast.Name(imports.name(RUNTIME, "rebuild_models")), call("globals"))))
    exports = sorted(names)
    header: list[ast.stmt] = [
        doc_expr(f"Models for the {plan.client.api_name}."),
        *imports.statements(),
        ast.Assign([ast.Name("__all__")], ast.List([const(n) for n in exports])),
    ]
    return ast.Module(body=[*header, *body, *tail], type_ignores=[]), exports

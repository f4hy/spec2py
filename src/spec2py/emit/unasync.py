"""Derive sync code from async code mechanically (the approach ``unasync`` popularized).

Resources and clients are generated once, as async AST. This transformer turns
a freshly built copy into the sync variant: ``async def`` becomes ``def``, ``await x``
becomes ``x``, ``async for``/``async with`` lose ``async``, and async names are
renamed through an explicit table. The two variants can never drift.
"""

import ast
from collections.abc import Callable, Mapping
from typing import Final

from ..plan import RUNTIME
from .pyast import Imports, const, doc_expr

__all__ = ["RUNTIME_RENAMES", "to_sync", "twin_module"]

RUNTIME_RENAMES: Final = {
    "AsyncAPIClient": "APIClient",
    "AsyncAPIResource": "APIResource",
    "AsyncPaginator": "Page",
    "AsyncPage": "Page",
    "AsyncStreamManager": "StreamManager",
    "AsyncStream": "Stream",
    "AsyncClient": "Client",
    "AsyncBaseTransport": "BaseTransport",
    "async_to_raw_response_wrapper": "to_raw_response_wrapper",
    "aclose": "close",
    "__aenter__": "__enter__",
    "__aexit__": "__exit__",
}


class _Unasync(ast.NodeTransformer):
    def __init__(self, renames: Mapping[str, str]) -> None:
        self.renames = renames

    def _rename(self, name: str) -> str:
        return self.renames.get(name, name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        fields = {f: getattr(node, f) for f in node._fields}
        fields["name"] = self._rename(node.name)
        return ast.copy_location(ast.FunctionDef(**fields), node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        node.name = self._rename(node.name)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.generic_visit(node)
        node.name = self._rename(node.name)
        return node

    def visit_Await(self, node: ast.Await) -> ast.expr:
        return self.visit(node.value)  # type: ignore[no-any-return]

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.For:
        self.generic_visit(node)
        fields = {f: getattr(node, f) for f in node._fields}
        return ast.copy_location(ast.For(**fields), node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.With:
        self.generic_visit(node)
        fields = {f: getattr(node, f) for f in node._fields}
        return ast.copy_location(ast.With(**fields), node)

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self._rename(node.id)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.Attribute:
        self.generic_visit(node)
        node.attr = self._rename(node.attr)
        return node


def to_sync[N: ast.AST](node: N, renames: Mapping[str, str]) -> N:
    """Turn async ``node`` into sync in place, renaming via ``renames`` + runtime names."""
    table = {**RUNTIME_RENAMES, **renames}
    return _Unasync(table).visit(node)  # type: ignore[no-any-return]


def twin_module(
    doc: str,
    imports: Imports,
    renames: Mapping[str, str],
    *builders: Callable[[], ast.ClassDef],
) -> ast.Module:
    """A module with each built async class preceded by its sync twin.

    Each builder runs twice (building is cheaper than ``copy.deepcopy``), so it
    must be deterministic.
    """
    classes: list[ast.ClassDef] = []
    for build in builders:
        async_cls = build()
        classes += [to_sync(build(), renames), async_cls]
    # The sync twins use the sync runtime names; import those too.
    for (module, name), _ in imports.used():
        if module == RUNTIME and name in RUNTIME_RENAMES:
            imports.name(RUNTIME, RUNTIME_RENAMES[name])
    return ast.Module(
        body=[
            doc_expr(doc),
            *imports.statements(),
            ast.Assign([ast.Name("__all__")], const([c.name for c in classes])),
            *classes,
        ],
        type_ignores=[],
    )

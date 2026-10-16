"""Derive sync code from async code mechanically (the approach ``unasync`` popularized).

Resources and clients are generated once, as async AST. This transformer turns
a deep copy into the sync variant: ``async def`` becomes ``def``, ``await x``
becomes ``x``, ``async for``/``async with`` lose ``async``, and async names are
renamed through an explicit table. The two variants can never drift.
"""

import ast
import copy
from collections.abc import Mapping
from typing import Final

__all__ = ["RUNTIME_RENAMES", "to_sync"]

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

    def visit_arg(self, node: ast.arg) -> ast.arg:
        self.generic_visit(node)
        return node


def to_sync[N: ast.AST](node: N, renames: Mapping[str, str]) -> N:
    """Return a sync copy of async ``node``, renaming via ``renames`` + runtime names."""
    table = {**RUNTIME_RENAMES, **renames}
    return _Unasync(table).visit(copy.deepcopy(node))  # type: ignore[no-any-return]

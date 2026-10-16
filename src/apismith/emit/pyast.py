"""Python type expressions and AST-building helpers.

The emitter builds every module as an ``ast.Module`` (never string templates),
so generated code is syntactically valid by construction. Type annotations are
small ``TypeExpr`` trees that render to AST and record the imports they need.
"""

import ast
import textwrap
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

try:  # Python 3.14 moved the unparser out of ast.py
    from _ast_unparse import Unparser as _StdUnparser  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _StdUnparser = ast._Unparser  # type: ignore[attr-defined]

__all__ = [
    "MODELS",
    "Imports",
    "TAnnotated",
    "TCall",
    "TLiteral",
    "TName",
    "TNone",
    "TSub",
    "TUnion",
    "TypeExpr",
    "docstring",
    "unparse",
]

MODELS: Final = "<models>"
"""Pseudo-module for names defined in the generated ``models.py``."""


class Imports:
    """Collects ``from x import y`` statements and resolves local-name clashes."""

    def __init__(
        self, *, package: str, here: str, reserved: Iterable[str] = ()
    ) -> None:
        self.package = package
        self.here = here
        self._names: dict[tuple[str, str], str] = {}
        self._taken: set[str] = set(reserved)
        self._modules: set[str] = set()
        self._preassigned: dict[tuple[str, str], str] = {}
        self.shadowed: set[str] = set()
        """Builtins shadowed by a class attribute (a method named ``list``)."""
        self.model_modules: dict[str, str] | None = None
        """When models are split into a package: model name -> its module."""
        self.deferred: set[tuple[str, str]] = set()
        """Imports needed only by type checkers (emitted under ``TYPE_CHECKING``)."""

    def preassign(self, module: str, *names: str) -> None:
        """Claim local names for imports that may be used later (e.g. by unasync)."""
        for name in names:
            self._preassigned[(module, name)] = name
            self._taken.add(name)

    def name(self, module: str | None, name: str) -> str:
        """Return the local name for ``module.name``, importing it if needed."""
        if module == MODELS and self.model_modules is not None:
            target = self.model_modules[name]
            if self.here == f"models.{target}":
                return name
            module = f".{target}"
        if module is None or module == self.here:
            return name
        key = (module, name)
        if key in self._names:
            return self._names[key]
        if key in self._preassigned:
            self._names[key] = self._preassigned[key]
            return name
        local = name
        if local in self._taken:
            local = f"{name}Model" if module == MODELS else f"_{name}"
            n = 2
            while local in self._taken:
                local = f"{name}_{n}"
                n += 1
        self._taken.add(local)
        self._names[key] = local
        return local

    def used(self) -> list[tuple[tuple[str, str], str]]:
        return sorted(self._names.items())

    def module(self, module: str) -> str:
        self._modules.add(module)
        return module

    def reserve(self, *names: str) -> None:
        self._taken.update(names)

    def _resolve(self, module: str) -> str:
        if module != MODELS:
            return module
        depth = self.here.count(".")
        return "." * (depth + 1) + "models" if depth else ".models"

    def statements(self) -> list[ast.stmt]:
        grouped: dict[str, list[ast.alias]] = defaultdict(list)
        deferred: dict[str, list[ast.alias]] = defaultdict(list)
        for (module, name), local in sorted(self._names.items()):
            target = deferred if (module, name) in self.deferred else grouped
            target[self._resolve(module)].append(
                ast.alias(name=name, asname=None if local == name else local)
            )
        if deferred:
            grouped["typing"].append(ast.alias(name="TYPE_CHECKING"))
        out: list[ast.stmt] = [
            ast.Import(names=[ast.alias(name=m)]) for m in sorted(self._modules)
        ]
        out.extend(self._from_imports(grouped))
        if deferred:
            out.append(
                ast.If(ast.Name("TYPE_CHECKING"), self._from_imports(deferred), [])
            )
        return out

    @staticmethod
    def _from_imports(grouped: dict[str, list[ast.alias]]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for module, aliases in sorted(grouped.items()):
            level = len(module) - len(module.lstrip("."))
            out.append(
                ast.ImportFrom(
                    module=module.lstrip(".") or None, names=aliases, level=level
                )
            )
        return out


class TypeExpr:
    """A Python type annotation."""

    def static(self) -> TypeExpr:
        """This type without ``Annotated`` metadata (which only pydantic reads)."""
        return self

    def to_ast(self, imports: Imports) -> ast.expr:
        raise NotImplementedError

    def render(self) -> str:
        """Readable form, for docs, diffs and snapshots."""
        raise NotImplementedError

    @property
    def is_none(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class TName(TypeExpr):
    name: str
    module: str | None = None

    def to_ast(self, imports: Imports) -> ast.expr:
        if self.module is None and self.name in imports.shadowed:
            return ast.Attribute(ast.Name(imports.module("builtins")), self.name)
        return ast.Name(imports.name(self.module, self.name))

    def render(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class TNone(TypeExpr):
    def to_ast(self, imports: Imports) -> ast.expr:
        return ast.Constant(None)

    def render(self) -> str:
        return "None"

    @property
    def is_none(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class TSub(TypeExpr):
    base: TypeExpr
    args: tuple[TypeExpr, ...]

    def static(self) -> TypeExpr:
        return TSub(self.base, tuple(a.static() for a in self.args))

    def to_ast(self, imports: Imports) -> ast.expr:
        args = [a.to_ast(imports) for a in self.args]
        slice_: ast.expr = args[0] if len(args) == 1 else ast.Tuple(args)
        return ast.Subscript(self.base.to_ast(imports), slice_)

    def render(self) -> str:
        return f"{self.base.render()}[{', '.join(a.render() for a in self.args)}]"


@dataclass(frozen=True, slots=True)
class TUnion(TypeExpr):
    members: tuple[TypeExpr, ...]

    @staticmethod
    def of(*members: TypeExpr) -> TypeExpr:
        flat: list[TypeExpr] = []
        literal_values: list[Any] = []
        for m in members:
            for inner in m.members if isinstance(m, TUnion) else (m,):
                if isinstance(inner, TLiteral):
                    # One Literal[...] reads better than a union of them.
                    if not literal_values:
                        flat.append(inner)
                    literal_values.extend(
                        v for v in inner.values if v not in literal_values
                    )
                elif inner not in flat:
                    flat.append(inner)
        if literal_values:
            flat = [
                TLiteral(tuple(literal_values)) if isinstance(m, TLiteral) else m
                for m in flat
            ]
        # None and NotGiven go last, as people write them.
        flat.sort(
            key=lambda t: (
                2 if t == TName("NotGiven", "apismith_runtime") else int(t.is_none)
            )
        )
        return flat[0] if len(flat) == 1 else TUnion(tuple(flat))

    def static(self) -> TypeExpr:
        return TUnion.of(*(m.static() for m in self.members))

    def to_ast(self, imports: Imports) -> ast.expr:
        nodes = [m.to_ast(imports) for m in self.members]
        out = nodes[0]
        for node in nodes[1:]:
            out = ast.BinOp(out, ast.BitOr(), node)
        return out

    def render(self) -> str:
        return " | ".join(m.render() for m in self.members)


@dataclass(frozen=True, slots=True)
class TLiteral(TypeExpr):
    values: tuple[Any, ...]

    def to_ast(self, imports: Imports) -> ast.expr:
        consts: list[ast.expr] = [ast.Constant(v) for v in self.values]
        slice_ = consts[0] if len(consts) == 1 else ast.Tuple(consts)
        return ast.Subscript(ast.Name(imports.name("typing", "Literal")), slice_)

    def render(self) -> str:
        return f"Literal[{', '.join(repr(v) for v in self.values)}]"


@dataclass(frozen=True, slots=True)
class TCall(TypeExpr):
    """A call used as annotation metadata, e.g. ``Field(discriminator="type")``."""

    func: TName
    kwargs: tuple[tuple[str, Any], ...] = ()

    def to_ast(self, imports: Imports) -> ast.expr:
        return ast.Call(
            self.func.to_ast(imports),
            args=[],
            keywords=[ast.keyword(k, ast.Constant(v)) for k, v in self.kwargs],
        )

    def render(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.kwargs)
        return f"{self.func.render()}({args})"


@dataclass(frozen=True, slots=True)
class TAnnotated(TypeExpr):
    inner: TypeExpr
    metadata: tuple[TypeExpr, ...] = field(default=())

    def to_ast(self, imports: Imports) -> ast.expr:
        elts = [self.inner.to_ast(imports), *(m.to_ast(imports) for m in self.metadata)]
        return ast.Subscript(
            ast.Name(imports.name("typing", "Annotated")), ast.Tuple(elts)
        )

    def static(self) -> TypeExpr:
        return self.inner.static()

    def render(self) -> str:
        meta = ", ".join(m.render() for m in self.metadata)
        return f"Annotated[{self.inner.render()}, {meta}]"


# -- AST helpers -----------------------------------------------------------------


def docstring(*paragraphs: str | None, indent: int = 4, width: int = 80) -> str | None:
    """Join and wrap paragraphs into docstring text (``None`` if all empty).

    ``indent`` is the column the docstring sits at, so continuation lines and the
    closing quotes line up once unparsed.
    """
    out: list[str] = []
    for para in paragraphs:
        if not para or not para.strip():
            continue
        for block in para.strip().split("\n\n"):
            lines = block.splitlines()
            if any(
                line.startswith(("  ", "- ", "* ", "|", "```")) for line in lines
            ) or (len(lines) > 1 and all(len(line) < 60 for line in lines)):
                out.append("\n".join(line.rstrip() for line in lines))
            else:
                out.append(
                    textwrap.fill(
                        " ".join(line.strip() for line in lines), width=width - indent
                    )
                )
    if not out:
        return None
    text = "\n\n".join(out)
    if "\n" not in text:
        return text
    pad = " " * indent
    lines = text.split("\n")
    return (
        "\n".join([lines[0], *(pad + line if line else line for line in lines[1:])])
        + "\n"
        + pad
    )


def doc_expr(text: str) -> ast.Expr:
    return ast.Expr(ast.Constant(text))


class _Unparser(_StdUnparser):  # type: ignore[misc]
    """Renders attribute docstrings (string statements after fields) triple-quoted."""

    def visit_Expr(self, node: ast.Expr) -> None:
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            self._write_docstring(value)
            return
        super().visit_Expr(node)


def unparse(module: ast.Module) -> str:
    ast.fix_missing_locations(module)
    return str(_Unparser().visit(module))


def call(func: ast.expr | str, *args: ast.expr, **kwargs: ast.expr) -> ast.Call:
    f = ast.Name(func) if isinstance(func, str) else func
    return ast.Call(f, list(args), [ast.keyword(k, v) for k, v in kwargs.items()])


def attr(*parts: str) -> ast.expr:
    node: ast.expr = ast.Name(parts[0])
    for part in parts[1:]:
        node = ast.Attribute(node, part)
    return node


def const(value: Any) -> ast.expr:
    if isinstance(value, dict):
        return ast.Dict([const(k) for k in value], [const(v) for v in value.values()])
    if isinstance(value, list | tuple):
        return ast.List([const(v) for v in value])
    return ast.Constant(value)


def arg(name: str, annotation: ast.expr | None = None) -> ast.arg:
    return ast.arg(name, annotation)


def names_in(nodes: Sequence[ast.AST]) -> set[str]:
    return {n.id for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name)}

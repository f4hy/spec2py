"""A small RFC 9535 JSONPath engine that returns mutable locations.

Overlays need to *change* what a path selects, so every match carries its
parent container and key. Supported: ``$``, ``@``, ``.name``, ``['name']``,
``*``, ``..`` (descendants), indices, slices, unions, and filters with
comparisons, ``&&``, ``||``, ``!``, existence tests and the ``length()``,
``count()``, ``match()`` and ``search()`` functions. The legacy ``[?(...)]``
form works because parentheses group.
"""

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

__all__ = ["JSONPathError", "Location", "compile_path", "find"]

type JSON = Any


class JSONPathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Location:
    """One match: its value, the container holding it and its key there."""

    value: JSON
    parent: JSON
    key: str | int | None
    path: tuple[str | int, ...]

    def set(self, value: JSON) -> None:
        if self.parent is None or self.key is None:
            raise JSONPathError("cannot replace the document root")
        self.parent[self.key] = value


# -- tokenizer -------------------------------------------------------------------

_TOKEN_RE: Final = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<op>\.\.|==|!=|<=|>=|&&|\|\||[$@.\[\]*,:?()!<>])
  | (?P<name>[A-Za-z_\u0080-\U0010ffff][\w\u0080-\U0010ffff-]*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str
    text: str
    pos: int


def _tokenize(source: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    pos = 0
    while pos < len(source):
        m = _TOKEN_RE.match(source, pos)
        if not m:
            raise JSONPathError(f"unexpected character {source[pos]!r} at {pos}")
        kind = cast(str, m.lastgroup)
        if kind != "ws":
            tokens.append(_Tok(kind, m.group(), pos))
        pos = m.end()
    tokens.append(_Tok("eof", "", pos))
    return tokens


def _unquote(text: str) -> str:
    body = text[1:-1]
    return re.sub(
        r"\\(u[0-9a-fA-F]{4}|.)",
        lambda m: (
            chr(int(m.group(1)[1:], 16))
            if m.group(1).startswith("u")
            else {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(
                m.group(1), m.group(1)
            )
        ),
        body,
    )


# -- AST -------------------------------------------------------------------------

type _Node = tuple[JSON, JSON, str | int | None, tuple[str | int, ...]]
type _Selector = Callable[[_Node, JSON], Iterator[_Node]]


def _children(node: _Node) -> Iterator[_Node]:
    value, _, _, path = node
    if isinstance(value, dict):
        for k, v in cast(dict[str, JSON], value).items():
            yield (v, value, k, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(cast(list[JSON], value)):
            yield (v, value, i, (*path, i))


def _descendants(node: _Node) -> Iterator[_Node]:
    yield node
    for child in _children(node):
        yield from _descendants(child)


def _name_selector(name: str) -> _Selector:
    def select(node: _Node, root: JSON) -> Iterator[_Node]:
        value, _, _, path = node
        if isinstance(value, dict) and name in value:
            yield (value[name], value, name, (*path, name))

    return select


def _wildcard(node: _Node, root: JSON) -> Iterator[_Node]:
    yield from _children(node)


def _index_selector(index: int) -> _Selector:
    def select(node: _Node, root: JSON) -> Iterator[_Node]:
        value, _, _, path = node
        if isinstance(value, list):
            items = cast(list[JSON], value)
            i = index + len(items) if index < 0 else index
            if 0 <= i < len(items):
                yield (items[i], value, i, (*path, i))

    return select


def _slice_selector(start: int | None, stop: int | None, step: int | None) -> _Selector:
    def select(node: _Node, root: JSON) -> Iterator[_Node]:
        value, _, _, path = node
        if isinstance(value, list):
            items = cast(list[JSON], value)
            for i in range(len(items))[slice(start, stop, step)]:
                yield (items[i], value, i, (*path, i))

    return select


type _Expr = Callable[[_Node, JSON], JSON]
_NOTHING: Final = object()


def _filter_selector(expr: _Expr) -> _Selector:
    def select(node: _Node, root: JSON) -> Iterator[_Node]:
        for child in _children(node):
            if _truthy(expr(child, root)):
                yield child

    return select


def _truthy(value: JSON) -> bool:
    if isinstance(value, _NodeList):
        return bool(value.nodes)
    return value is True


class _NodeList:
    """The result of a path inside a filter (distinct from a JSON value)."""

    __slots__ = ("nodes",)

    def __init__(self, nodes: list[_Node]) -> None:
        self.nodes = nodes

    def single(self) -> JSON:
        return self.nodes[0][0] if len(self.nodes) == 1 else _NOTHING


def _value(v: JSON) -> JSON:
    return v.single() if isinstance(v, _NodeList) else v


def _compare(op: str, left: JSON, right: JSON) -> bool:
    a, b = _value(left), _value(right)
    if op in ("==", "!="):
        equal = (
            a is b
            if a is _NOTHING or b is _NOTHING
            else (type(a) is type(b) or _both_numbers(a, b)) and a == b
        )
        return equal if op == "==" else not equal
    if a is _NOTHING or b is _NOTHING:
        return False
    if _both_numbers(a, b) or (isinstance(a, str) and isinstance(b, str)):
        match op:
            case "<":
                return bool(a < b)
            case "<=":
                return bool(a <= b)
            case ">":
                return bool(a > b)
            case ">=":
                return bool(a >= b)
    return False


def _both_numbers(a: JSON, b: JSON) -> bool:
    return all(isinstance(x, int | float) and not isinstance(x, bool) for x in (a, b))


def _fn_length(args: Sequence[JSON]) -> JSON:
    v = _value(args[0])
    if isinstance(v, str | list | dict):
        return len(cast(Sequence[Any], v))
    return _NOTHING


def _fn_count(args: Sequence[JSON]) -> JSON:
    v = args[0]
    return len(v.nodes) if isinstance(v, _NodeList) else _NOTHING


def _fn_match(args: Sequence[JSON]) -> JSON:
    s, pattern = _value(args[0]), _value(args[1])
    if isinstance(s, str) and isinstance(pattern, str):
        return re.fullmatch(pattern, s) is not None
    return False


def _fn_search(args: Sequence[JSON]) -> JSON:
    s, pattern = _value(args[0]), _value(args[1])
    if isinstance(s, str) and isinstance(pattern, str):
        return re.search(pattern, s) is not None
    return False


_FUNCTIONS: Final[dict[str, Callable[[Sequence[JSON]], JSON]]] = {
    "length": _fn_length,
    "count": _fn_count,
    "match": _fn_match,
    "search": _fn_search,
    "value": lambda args: _value(args[0]),
}


# -- parser ----------------------------------------------------------------------


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = _tokenize(source)
        self.i = 0

    @property
    def tok(self) -> _Tok:
        return self.tokens[self.i]

    def take(self, text: str | None = None, kind: str | None = None) -> _Tok:
        tok = self.tok
        if (text is not None and tok.text != text) or (
            kind is not None and tok.kind != kind
        ):
            want = text or kind
            raise JSONPathError(
                f"expected {want!r} at {tok.pos} in {self.source!r}, got {tok.text!r}"
            )
        self.i += 1
        return tok

    def accept(self, text: str) -> bool:
        if self.tok.text == text and self.tok.kind in ("op", "name"):
            self.i += 1
            return True
        return False

    def parse_query(self, anchor: str) -> list[tuple[bool, list[_Selector]]]:
        self.take(anchor)
        return self.parse_segments()

    def parse_segments(self) -> list[tuple[bool, list[_Selector]]]:
        segments: list[tuple[bool, list[_Selector]]] = []
        while True:
            if self.accept(".."):
                if self.tok.text == "[":
                    segments.append((True, self.parse_bracket()))
                elif self.accept("*"):
                    segments.append((True, [_wildcard]))
                else:
                    segments.append((True, [_name_selector(self.take(kind="name").text)]))
            elif self.accept("."):
                if self.accept("*"):
                    segments.append((False, [_wildcard]))
                else:
                    segments.append((False, [_name_selector(self.take(kind="name").text)]))
            elif self.tok.text == "[":
                segments.append((False, self.parse_bracket()))
            else:
                return segments

    def parse_bracket(self) -> list[_Selector]:
        self.take("[")
        selectors = [self.parse_selector()]
        while self.accept(","):
            selectors.append(self.parse_selector())
        self.take("]")
        return selectors

    def parse_selector(self) -> _Selector:
        tok = self.tok
        if tok.kind == "string":
            self.i += 1
            return _name_selector(_unquote(tok.text))
        if self.accept("*"):
            return _wildcard
        if self.accept("?"):
            return _filter_selector(self.parse_or())
        if tok.kind == "number" or tok.text == ":":
            return self.parse_index_or_slice()
        raise JSONPathError(f"bad selector {tok.text!r} at {tok.pos} in {self.source!r}")

    def parse_index_or_slice(self) -> _Selector:
        parts: list[int | None] = [None]
        if self.tok.kind == "number":
            parts[0] = int(self.take(kind="number").text)
        if self.tok.text != ":":
            assert parts[0] is not None
            return _index_selector(parts[0])
        while self.accept(":"):
            if self.tok.kind == "number":
                parts.append(int(self.take(kind="number").text))
            else:
                parts.append(None)
        start, stop, step = (parts + [None, None])[:3]
        return _slice_selector(start, stop, step)

    # filter expressions
    def parse_or(self) -> _Expr:
        left = self.parse_and()
        while self.accept("||"):
            right = self.parse_and()
            left = (lambda lft, rgt: lambda n, r: _truthy(lft(n, r)) or _truthy(rgt(n, r)))(
                left, right
            )
        return left

    def parse_and(self) -> _Expr:
        left = self.parse_not()
        while self.accept("&&"):
            right = self.parse_not()
            left = (lambda lft, rgt: lambda n, r: _truthy(lft(n, r)) and _truthy(rgt(n, r)))(
                left, right
            )
        return left

    def parse_not(self) -> _Expr:
        if self.accept("!"):
            inner = self.parse_not()
            return lambda n, r: not _truthy(inner(n, r))
        return self.parse_comparison()

    def parse_comparison(self) -> _Expr:
        left = self.parse_primary()
        if self.tok.text in ("==", "!=", "<", "<=", ">", ">="):
            op = self.take().text
            right = self.parse_primary()
            return lambda n, r: _compare(op, left(n, r), right(n, r))
        return left

    def parse_primary(self) -> _Expr:
        tok = self.tok
        if self.accept("("):
            inner = self.parse_or()
            self.take(")")
            return inner
        if tok.kind == "string":
            self.i += 1
            text = _unquote(tok.text)
            return lambda n, r: text
        if tok.kind == "number":
            self.i += 1
            num: float | int = float(tok.text) if any(c in tok.text for c in ".eE") else int(tok.text)
            return lambda n, r: num
        if tok.kind == "name" and tok.text in ("true", "false", "null"):
            self.i += 1
            const = {"true": True, "false": False, "null": None}[tok.text]
            return lambda n, r: const
        if tok.text in ("@", "$"):
            relative = tok.text == "@"
            segments = self.parse_query(tok.text)

            def run(n: _Node, r: JSON) -> JSON:
                start: _Node = n if relative else (r, None, None, ())
                return _NodeList(_evaluate(start, r, segments))

            return run
        if tok.kind == "name" and self.tokens[self.i + 1].text == "(":
            name = self.take(kind="name").text
            fn = _FUNCTIONS.get(name)
            if fn is None:
                raise JSONPathError(f"unknown function {name}()")
            self.take("(")
            args: list[_Expr] = []
            if self.tok.text != ")":
                args.append(self.parse_or())
                while self.accept(","):
                    args.append(self.parse_or())
            self.take(")")
            return lambda n, r: fn([a(n, r) for a in args])
        raise JSONPathError(f"unexpected {tok.text!r} at {tok.pos} in {self.source!r}")


def _evaluate(
    start: _Node, root: JSON, segments: list[tuple[bool, list[_Selector]]]
) -> list[_Node]:
    nodes = [start]
    for descendant, selectors in segments:
        sources = [d for n in nodes for d in _descendants(n)] if descendant else nodes
        nodes = [out for n in sources for sel in selectors for out in sel(n, root)]
    return nodes


@dataclass(frozen=True, slots=True)
class CompiledPath:
    source: str
    segments: list[tuple[bool, list[_Selector]]]

    def find(self, document: JSON) -> list[Location]:
        nodes = _evaluate((document, None, None, ()), document, self.segments)
        return [Location(v, p, k, path) for v, p, k, path in nodes]


def compile_path(source: str) -> CompiledPath:
    parser = _Parser(source.strip())
    segments = parser.parse_query("$")
    if parser.tok.kind != "eof":
        raise JSONPathError(f"unexpected {parser.tok.text!r} at {parser.tok.pos}")
    return CompiledPath(source, segments)


def find(document: JSON, path: str) -> list[Location]:
    """Return every location ``path`` selects in ``document``."""
    return compile_path(path).find(document)

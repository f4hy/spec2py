"""``$ref`` handling: bundle external references into one document, resolve local ones.

External schema references (``other.yaml#/components/schemas/Pet``, or a whole
file that is a schema) are hoisted into ``#/components/schemas`` under a
collision-free name; other external references (parameters, responses, path
items) are inlined. Afterwards every ``$ref`` in the document is local.
"""

import copy
import pathlib
import urllib.parse
from collections.abc import Callable
from typing import Any, cast

from ..loader import fetch_text, is_url, parse_document

__all__ = ["RefError", "bundle", "resolve_pointer", "unescape_pointer"]

type JSON = Any

_SCHEMA_HINTS = frozenset(
    {"type", "properties", "allOf", "oneOf", "anyOf", "items", "enum", "$ref", "additionalProperties", "const"}
)


class RefError(ValueError):
    pass


def unescape_pointer(token: str) -> str:
    return urllib.parse.unquote(token).replace("~1", "/").replace("~0", "~")


def resolve_pointer(document: JSON, pointer: str) -> JSON:
    """Resolve a JSON Pointer fragment (``/components/schemas/Pet``)."""
    node = document
    if pointer in ("", "/"):
        return node
    for raw in pointer.lstrip("/").split("/"):
        token = unescape_pointer(raw)
        if isinstance(node, dict) and token in node:
            node = cast(dict[str, JSON], node)[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(cast(list[JSON], node)):
            node = cast(list[JSON], node)[int(token)]
        else:
            raise RefError(f"cannot resolve #{pointer}")
    return node


def _join(base: str, ref_doc: str) -> str:
    if is_url(ref_doc):
        return ref_doc
    if is_url(base):
        return urllib.parse.urljoin(base, ref_doc)
    return str((pathlib.Path(base).parent / ref_doc).resolve())


class _Bundler:
    def __init__(self, root: dict[str, JSON], loader: Callable[[str], JSON]) -> None:
        self.root = root
        self.loader = loader
        self.cache: dict[str, JSON] = {}
        self.hoisted: dict[tuple[str, str], str] = {}
        schemas = root.setdefault("components", {}).setdefault("schemas", {})
        self.schemas = cast(dict[str, JSON], schemas)

    def document(self, uri: str) -> JSON:
        if uri not in self.cache:
            self.cache[uri] = self.loader(uri)
        return self.cache[uri]

    def name_for(self, uri: str, pointer: str) -> str:
        base = pointer.rstrip("/").rsplit("/", 1)[-1] if pointer else pathlib.PurePosixPath(urllib.parse.urlparse(uri).path).stem
        base = unescape_pointer(base) or "Schema"
        name, n = base, 2
        while name in self.schemas:
            name, n = f"{base}{n}", n + 1
        return name

    def walk(self, node: JSON, base: str, *, external: bool) -> JSON:
        """Rewrite refs in ``node`` (from document ``base``) to be local to the root."""
        if isinstance(node, list):
            return [self.walk(v, base, external=external) for v in cast(list[JSON], node)]
        if not isinstance(node, dict):
            return node
        d = cast(dict[str, JSON], node)
        ref = d.get("$ref")
        if isinstance(ref, str):
            siblings = {k: self.walk(v, base, external=external) for k, v in d.items() if k != "$ref"}
            local = self.ref(ref, base, external=external)
            if isinstance(local, dict) and "$ref" in local:
                return {**cast(dict[str, JSON], local), **siblings}
            if isinstance(local, dict):
                return {**cast(dict[str, JSON], local), **siblings}
            return local
        return {k: self.walk(v, base, external=external) for k, v in d.items()}

    def ref(self, ref: str, base: str, *, external: bool) -> JSON:
        doc_part, _, pointer = ref.partition("#")
        if not doc_part and not external:
            return {"$ref": ref}
        uri = _join(base, doc_part) if doc_part else base
        key = (uri, pointer)
        if key in self.hoisted:
            return {"$ref": f"#/components/schemas/{self.hoisted[key]}"}
        document = self.document(uri)
        try:
            target = resolve_pointer(document, pointer)
        except RefError:
            # A Swagger 2.0 document referenced from a converted one.
            legacy = pointer.replace("/components/schemas/", "/definitions/", 1)
            target = resolve_pointer(document, legacy)
        is_schema = "/schemas/" in pointer or "/definitions/" in pointer or (
            isinstance(target, dict) and bool(_SCHEMA_HINTS & set(cast(dict[str, JSON], target)))
            and not pointer.startswith(("/paths", "/components/parameters", "/components/responses", "/components/requestBodies"))
        )
        if is_schema:
            name = self.name_for(uri, pointer)
            self.hoisted[key] = name
            self.schemas[name] = {}  # placeholder so recursive refs see the name
            self.schemas[name] = self.walk(copy.deepcopy(target), uri, external=True)
            return {"$ref": f"#/components/schemas/{name}"}
        return self.walk(copy.deepcopy(target), uri, external=True)


def _default_loader(uri: str) -> JSON:
    if is_url(uri):
        return parse_document(fetch_text(uri), uri)
    path = pathlib.Path(uri)
    if not path.is_file():
        raise RefError(f"referenced document not found: {uri}")
    return parse_document(path.read_text(encoding="utf-8"), uri)


def bundle(
    document: dict[str, JSON],
    base_uri: str,
    loader: Callable[[str], JSON] = _default_loader,
) -> dict[str, JSON]:
    """Return ``document`` with every external ``$ref`` made local."""
    if not _has_external_refs(document):
        return document
    root = copy.deepcopy(document)
    bundler = _Bundler(root, loader)
    bundler.cache[base_uri] = document
    for key in list(root):
        if key != "components":
            root[key] = bundler.walk(root[key], base_uri, external=False)
    components = cast(dict[str, JSON], root["components"])
    for section, entries in list(components.items()):
        if not isinstance(entries, dict):
            continue
        for name in list(cast(dict[str, JSON], entries)):
            value = cast(dict[str, JSON], entries)[name]
            cast(dict[str, JSON], components[section])[name] = bundler.walk(value, base_uri, external=False)
    return root


def _has_external_refs(node: JSON) -> bool:
    if isinstance(node, dict):
        d = cast(dict[str, JSON], node)
        ref = d.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            return True
        return any(_has_external_refs(v) for v in d.values())
    if isinstance(node, list):
        return any(_has_external_refs(v) for v in cast(list[JSON], node))
    return False

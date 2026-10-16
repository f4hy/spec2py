"""Stable, collision-free, Pythonic names."""

import keyword
import re
import unicodedata
from collections.abc import Iterable

__all__ = [
    "NameRegistry",
    "camel_words",
    "pascal",
    "safe_identifier",
    "singular",
    "snake",
]


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def camel_words(text: str) -> list[str]:
    """Split ``"listHTTPInvoices_v2"`` into ``["list", "http", "invoices", "v2"]``."""
    text = _ascii(text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w]


def snake(text: str) -> str:
    return "_".join(camel_words(text))


def pascal(text: str) -> str:
    words = camel_words(text)
    return "".join(w[:1].upper() + w[1:] for w in words)


def safe_identifier(name: str, *, fallback: str = "value") -> str:
    """Make ``name`` a valid, non-keyword identifier (``class`` -> ``class_``)."""
    name = re.sub(r"\W", "_", _ascii(name))
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"n_{name}"
    if keyword.iskeyword(name) or name in ("None", "True", "False"):
        name = f"{name}_"
    return name


def singular(word: str) -> str:
    """Cheap English singular, good enough for ``line_items`` -> ``line_item``."""
    lower = word.lower()
    if lower.endswith("ies") and len(word) > 4:
        return word[:-3] + ("Y" if word[-3:].isupper() else "y")
    if lower.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


class NameRegistry:
    """Hands out unique names in one namespace, deterministically."""

    def __init__(self, reserved: Iterable[str] = ()) -> None:
        self._taken: set[str] = set(reserved)

    def __contains__(self, name: str) -> bool:
        return name in self._taken

    def claim(self, name: str, *, suffix: str = "") -> str:
        if name not in self._taken:
            self._taken.add(name)
            return name
        if suffix and f"{name}{suffix}" not in self._taken:
            name = f"{name}{suffix}"
            self._taken.add(name)
            return name
        n = 2
        while f"{name}{n}" in self._taken:
            n += 1
        self._taken.add(f"{name}{n}")
        return f"{name}{n}"

    def reserve(self, *names: str) -> None:
        self._taken.update(names)

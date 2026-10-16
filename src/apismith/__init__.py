"""apismith: a Python-only OpenAPI client generator.

The pipeline is a compiler with five stages::

    load -> normalize -> IR -> plan -> emit

Use the ``apismith`` command, or call :func:`generate` from Python.
"""

try:
    from ._version import __version__
except ImportError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

from .pipeline import Pipeline, generate, load_api

__all__ = ["Pipeline", "__version__", "generate", "load_api"]

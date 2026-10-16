import importlib
import pathlib
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest

from spec2py import generate
from spec2py.config import Config

SPECS = pathlib.Path(__file__).parent / "specs"
ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture(scope="session")
def specs() -> pathlib.Path:
    return SPECS


def generate_package(
    spec: str | pathlib.Path, out: pathlib.Path, **config: object
) -> pathlib.Path:
    """Generate ``spec`` with package layout into ``out``; return the package dir."""
    cfg = Config(layout="package", **config)  # type: ignore[arg-type]
    generated = generate(str(spec), out, cfg)
    return out / generated.package


def import_generated(out: pathlib.Path, package: str) -> ModuleType:
    for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[name]
    sys.path.insert(0, str(out))
    try:
        return importlib.import_module(package)
    finally:
        sys.path.remove(str(out))


@pytest.fixture(scope="session")
def acme_dir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    out = tmp_path_factory.mktemp("acme")
    generate_package(SPECS / "acme.yaml", out)
    return out


@pytest.fixture(scope="session")
def acme(acme_dir: pathlib.Path) -> Iterator[ModuleType]:
    sys.path.insert(0, str(acme_dir))
    try:
        yield importlib.import_module("acme")
    finally:
        sys.path.remove(str(acme_dir))

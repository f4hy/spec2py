"""Golden snapshots, determinism and regeneration safety of generated packages.

Set ``APISMITH_UPDATE_GOLDEN=1`` to rewrite the snapshots after a deliberate
change in output; the diff then shows up in review.
"""

import ast
import os
import pathlib

import pytest

from apismith import generate
from apismith.config import Config
from apismith.emit.package import MANIFEST, render_package
from apismith.pipeline import load_api

from .conftest import generate_package

GOLDEN = pathlib.Path(__file__).parent / "golden"


def snapshot(package_dir: pathlib.Path) -> dict[str, str]:
    return {
        str(p.relative_to(package_dir)): p.read_text()
        for p in sorted(package_dir.rglob("*"))
        if p.is_file()
        and p.name != MANIFEST
        and "_custom" not in p.parts
        and "__pycache__" not in p.parts
    }


@pytest.mark.parametrize(
    "spec", ["acme.yaml", "petstore_swagger2.json", "nullable_30.yaml"]
)
def test_golden_output(spec: str, specs: pathlib.Path, tmp_path: pathlib.Path) -> None:
    package = generate_package(specs / spec, tmp_path)
    actual = snapshot(package)
    golden = GOLDEN / spec.split(".", maxsplit=1)[0]
    if os.environ.get("APISMITH_UPDATE_GOLDEN") or not golden.exists():
        for old in golden.rglob("*") if golden.exists() else []:
            if old.is_file():
                old.unlink()
        for rel, text in actual.items():
            (golden / rel).parent.mkdir(parents=True, exist_ok=True)
            (golden / rel).write_text(text)
    assert actual == snapshot(golden)


def test_output_is_deterministic(specs: pathlib.Path, tmp_path: pathlib.Path) -> None:
    a = snapshot(generate_package(specs / "acme.yaml", tmp_path / "a"))
    b = snapshot(generate_package(specs / "acme.yaml", tmp_path / "b"))
    assert a == b
    manifest_a = (tmp_path / "a" / "acme" / MANIFEST).read_text()
    manifest_b = (tmp_path / "b" / "acme" / MANIFEST).read_text()
    assert manifest_a == manifest_b


def test_every_module_is_valid_python(specs: pathlib.Path) -> None:
    generated = render_package(load_api(str(specs / "acme.yaml")).plan)
    for rel, text in generated.files.items():
        if rel.endswith(".py"):
            ast.parse(text, filename=rel)


def test_sync_code_is_derived_from_async(acme_dir: pathlib.Path) -> None:
    source = (acme_dir / "acme" / "resources" / "invoices.py").read_text()
    tree = ast.parse(source)
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    sync_methods = [
        f.name for f in classes["Invoices"].body if isinstance(f, ast.FunctionDef)
    ]
    async_methods = [
        f.name
        for f in classes["AsyncInvoices"].body
        if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert sync_methods == async_methods
    assert not any(isinstance(n, ast.Await) for n in ast.walk(classes["Invoices"]))
    assert any(isinstance(n, ast.Await) for n in ast.walk(classes["AsyncInvoices"]))


def test_custom_directory_survives_and_is_reexported(
    specs: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    package = generate_package(specs / "acme.yaml", tmp_path)
    custom = package / "_custom" / "__init__.py"
    assert "__all__" in custom.read_text()
    custom.write_text(
        '__all__ = ["helper"]\n\n\ndef helper() -> str:\n    return "mine"\n'
    )
    (package / "_custom" / "extra.py").write_text("X = 1\n")
    generate_package(specs / "acme.yaml", tmp_path)
    assert 'return "mine"' in custom.read_text()
    assert (package / "_custom" / "extra.py").exists()
    init = (package / "__init__.py").read_text()
    assert "from ._custom import helper" in init
    assert '"helper"' in init


def test_stale_files_are_removed(specs: pathlib.Path, tmp_path: pathlib.Path) -> None:
    package = generate_package(specs / "acme.yaml", tmp_path)
    assert (package / "resources" / "events.py").exists()
    generate_package(
        specs / "acme.yaml", tmp_path, overlays=(str(specs / "acme_overlay.yaml"),)
    )
    assert not (package / "resources" / "events.py").exists()
    models = (package / "models.py").read_text()
    assert "vip: bool | None = None" in models
    assert "def void(" in (package / "resources" / "invoices.py").read_text()


def test_project_layout_scaffolds_once(
    specs: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    generate(str(specs / "acme.yaml"), tmp_path, Config())
    pyproject = tmp_path / "pyproject.toml"
    assert 'name = "acme"' in pyproject.read_text()
    assert (tmp_path / "src" / "acme" / "_client.py").exists()
    assert (tmp_path / "README.md").exists()
    pyproject.write_text("# mine\n")
    generate(str(specs / "acme.yaml"), tmp_path, Config())
    assert pyproject.read_text() == "# mine\n"


def test_large_specs_split_models_into_a_package(tmp_path: pathlib.Path) -> None:
    # A tree of references (each model points at its "parent"), like real specs.
    schemas = {
        f"Thing{i}": {
            "type": "object",
            "properties": {"next": {"$ref": f"#/components/schemas/Thing{i // 2}"}},
        }
        for i in range(450)
    }
    schemas["Base"] = {"type": "object", "properties": {"id": {"type": "string"}}}
    schemas["Child"] = {
        "allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"properties": {"x": {"type": "integer"}}},
        ]
    }
    schemas["Choice"] = {
        "oneOf": [{"$ref": "#/components/schemas/Thing1"}, {"type": "string"}]
    }
    spec = tmp_path / "big.json"
    import json

    spec.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "Big", "version": "1"},
                "paths": {},
                "components": {"schemas": schemas},
            }
        )
    )
    package = generate_package(spec, tmp_path / "out")
    files = sorted(p.name for p in (package / "models").iterdir())
    assert "_bases.py" in files and "_aliases.py" in files and "__init__.py" in files
    assert not (package / "models.py").exists()
    from .conftest import import_generated

    big = import_generated(tmp_path / "out", "big")
    t = big.models.Thing300.model_validate({"next": {"next": {}}})
    assert type(t.next).__name__ == "Thing150"
    assert type(t.next.next).__name__ == "Thing75"
    assert big.models.Child(id="a", x=1).id == "a"


def test_custom_resource_subclass_is_instantiated(
    specs: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    package = generate_package(specs / "acme.yaml", tmp_path)
    (package / "_custom" / "invoices.py").write_text(
        "from ..resources.invoices import AsyncInvoices as _AsyncInvoices\n"
        "from ..resources.invoices import Invoices as _Invoices\n\n\n"
        "class Invoices(_Invoices):\n"
        "    def total_open(self) -> int:\n"
        "        return sum(1 for _ in self.list(status='open'))\n\n\n"
        "class AsyncInvoices(_AsyncInvoices):\n"
        "    pass\n"
    )
    generate_package(
        specs / "acme.yaml",
        tmp_path,
        custom_resources={"invoices": "invoices:Invoices"},
    )
    from .conftest import import_generated

    acme = import_generated(tmp_path, "acme")
    client = acme.Acme(api_key="k")
    assert type(client.invoices).__module__ == "acme._custom.invoices"
    assert hasattr(client.invoices, "total_open")
    assert type(acme.AsyncAcme(api_key="k").invoices).__name__ == "AsyncInvoices"

"""Every generated client must pass strict mypy, strict pyright, ty and ruff with
zero suppressions. These run real type checkers, so they're marked slow."""

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from .conftest import generate_package

pytestmark = pytest.mark.slow

SPECS = [
    "acme.yaml",
    "petstore_swagger2.json",
    "nullable_30.yaml",
    "external/main.yaml",
]


def run(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture(scope="module", params=SPECS)
def generated(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    specs: pathlib.Path,
) -> pathlib.Path:
    out = tmp_path_factory.mktemp("gates")
    package = generate_package(specs / request.param, out)
    (out / "pyrightconfig.json").write_text(
        json.dumps({"typeCheckingMode": "strict", "pythonVersion": "3.14"})
    )
    return package


def test_mypy_strict(generated: pathlib.Path) -> None:
    result = run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", generated.name],
        generated.parent,
    )
    assert result.returncode == 0, result.stdout


def test_pyright_strict(generated: pathlib.Path) -> None:
    pyright = shutil.which("pyright") or str(
        pathlib.Path(sys.executable).parent / "pyright"
    )
    result = run([pyright, generated.name], generated.parent)
    assert result.returncode == 0, result.stdout


def test_ty(generated: pathlib.Path) -> None:
    ty = shutil.which("ty") or str(pathlib.Path(sys.executable).parent / "ty")
    result = run([ty, "check", generated.name], generated.parent)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ruff(generated: pathlib.Path) -> None:
    ruff = shutil.which("ruff") or str(pathlib.Path(sys.executable).parent / "ruff")
    lint = run(
        [ruff, "check", "--isolated", "--target-version", "py314", generated.name],
        generated.parent,
    )
    assert lint.returncode == 0, lint.stdout
    fmt = run(
        [
            ruff,
            "format",
            "--isolated",
            "--check",
            "--target-version",
            "py314",
            generated.name,
        ],
        generated.parent,
    )
    assert fmt.returncode == 0, fmt.stdout


def test_no_suppressions(generated: pathlib.Path) -> None:
    for path in generated.rglob("*.py"):
        text = path.read_text()
        assert (
            "type: ignore" not in text
            and "noqa" not in text
            and "pyright: ignore" not in text
        ), path

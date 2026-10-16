import json
import pathlib
import shutil

import pytest
import yaml

from apismith.cli import main
from apismith.config import ConfigError, load_config
from apismith.lint import lint
from apismith.pipeline import load_api
from apismith.surface import diff_surfaces, suggest_version


def write_project(
    tmp_path: pathlib.Path, specs: pathlib.Path, extra: str = ""
) -> pathlib.Path:
    shutil.copy(specs / "acme.yaml", tmp_path / "acme.yaml")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.apismith]\n"
        'spec = "acme.yaml"\n'
        'output = "client"\n'
        'layout = "package"\n' + extra
    )
    return tmp_path


def test_generate_check_and_update(
    tmp_path: pathlib.Path,
    specs: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = write_project(tmp_path, specs)
    monkeypatch.chdir(project)
    assert main(["generate"]) == 0
    assert (project / "client" / "acme" / "_client.py").exists()
    assert main(["generate", "--check"]) == 0
    assert "up to date" in capsys.readouterr().out

    # Edit the spec: --check fails, update prints the API diff and a version.
    spec = yaml.safe_load((project / "acme.yaml").read_text())
    del spec["paths"]["/events/export"]
    spec["components"]["schemas"]["Invoice"]["properties"]["paid_at"] = {
        "type": "string",
        "format": "date-time",
    }
    (project / "acme.yaml").write_text(yaml.safe_dump(spec))
    assert main(["update", "--check"]) == 1
    captured = capsys.readouterr()
    assert "events.export: method removed" in captured.out
    assert "models.Invoice.paid_at: field added" in captured.out
    assert "0.1.0 -> 0.2.0 (major)" in captured.out
    assert "out of date" in captured.err
    assert main(["update"]) == 0
    assert not (project / "client" / "acme" / "resources" / "events.py").exists()
    assert main(["generate", "--check"]) == 0


def test_hand_edits_are_detected_by_check(
    tmp_path: pathlib.Path,
    specs: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = write_project(tmp_path, specs)
    monkeypatch.chdir(project)
    main(["generate"])
    models = project / "client" / "acme" / "models.py"
    models.write_text(models.read_text() + "\n# hand edit\n")
    assert main(["generate", "--check"]) == 1
    assert "models.py" in capsys.readouterr().err


def test_config_from_pyproject(tmp_path: pathlib.Path, specs: pathlib.Path) -> None:
    project = write_project(
        tmp_path,
        specs,
        'client-name = "AcmeClient"\n'
        'overlays = ["fix.yaml"]\n'
        '[tool.apismith.rename-models]\nInvoice = "Bill"\n'
        '[tool.apismith.operations."GET /invoices"]\nname = "all"\npagination = false\n'
        "[tool.apismith.operations.exportEvents]\nexclude = true\n",
    )
    (project / "fix.yaml").write_text(
        "overlay: 1.0.0\ninfo: {title: f, version: '1'}\nactions: []\n"
    )
    config = load_config(project / "pyproject.toml")
    assert config.client_name == "AcmeClient"
    assert config.overlays == (str(project / "fix.yaml"),)
    assert config.spec == str(project / "acme.yaml")
    plan = load_api(config.spec, config).plan  # type: ignore[arg-type]
    invoices = next(r for r in plan.resources if r.attr == "invoices")
    assert invoices.methods[0].name == "all" and invoices.methods[0].pagination is None
    assert "events" not in {r.attr for r in plan.resources}
    assert any(m.name == "Bill" for m in plan.models)


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("bogus = 1\n", "unknown key"),
        ('[tool.apismith.operations.x]\nstream = "websocket"\n', "stream"),
        (
            '[tool.apismith.operations.x]\npagination = { style = "magic" }\n',
            "pagination.style",
        ),
    ],
)
def test_config_errors(tmp_path: pathlib.Path, snippet: str, message: str) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.apismith]\n" + snippet)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path / "pyproject.toml")


def test_cli_errors_are_reported(
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["generate", "missing.yaml"]) == 2
    assert "spec not found" in capsys.readouterr().err
    assert main(["generate"]) == 2
    assert "no spec given" in capsys.readouterr().err
    assert main(["fastapi", "nonexistent_module:app"]) == 2


def test_lint_command_and_fix_overlay(
    tmp_path: pathlib.Path, specs: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixes = tmp_path / "fixes.yaml"
    assert main(["lint", str(specs / "acme.yaml"), "--fix-overlay", str(fixes)]) == 0
    out = capsys.readouterr().out
    assert "missing-operation-id" in out
    overlay = yaml.safe_load(fixes.read_text())
    assert overlay["overlay"] == "1.0.0"
    assert {
        "target": "$.paths['/invoices/{invoice_id}/void'].post",
        "update": {"operationId": "createVoid"},
    } in overlay["actions"]
    # Applying the starter overlay removes the finding.
    assert (
        main(
            [
                "lint",
                str(specs / "acme.yaml"),
                "--overlay",
                str(fixes),
                "--format",
                "json",
            ]
        )
        == 0
    )
    codes = {f["code"] for f in json.loads(capsys.readouterr().out)}
    assert "missing-operation-id" not in codes
    assert main(["lint", str(specs / "acme.yaml"), "--strict"]) == 1


def test_lint_findings(specs: pathlib.Path) -> None:
    report = lint(load_api(str(specs / "acme.yaml")).api)
    codes = {f.code: f for f in report.findings}
    assert "missing-operation-id" in codes
    assert codes["union-without-discriminator"].locations
    assert "untyped-schema" in codes  # Product.attributes: {}
    assert report.ranked()[0].impact >= report.ranked()[-1].impact
    assert "--fix-overlay" in report.render()


def test_lint_suggests_discriminators(tmp_path: pathlib.Path) -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "x", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "Shape": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/Circle"},
                        {"$ref": "#/components/schemas/Square"},
                    ]
                },
                "Circle": {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "circle"},
                        "r": {"type": "number"},
                    },
                },
                "Square": {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "square"},
                        "side": {"type": "number"},
                    },
                },
            }
        },
    }
    path = tmp_path / "s.json"
    path.write_text(json.dumps(spec))
    report = lint(load_api(str(path)).api)
    finding = next(
        f for f in report.findings if f.code == "union-without-discriminator"
    )
    assert "could discriminate on `kind`" in finding.locations[0]
    assert finding.fix == [
        {
            "target": "$.components.schemas['Shape']",
            "update": {"discriminator": {"propertyName": "kind"}},
        }
    ]


def test_diff_command(
    tmp_path: pathlib.Path, specs: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    new = yaml.safe_load((specs / "acme.yaml").read_text())
    new["paths"]["/invoices"]["get"]["parameters"].append(
        {
            "name": "region",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
        }
    )
    new["components"]["schemas"]["Money"]["properties"]["amount"] = {"type": "number"}
    path = tmp_path / "new.yaml"
    path.write_text(yaml.safe_dump(new))
    assert (
        main(["diff", str(specs / "acme.yaml"), str(path), "--fail-on-breaking"]) == 1
    )
    out = capsys.readouterr().out
    assert "! invoices.list(region): required parameter added" in out
    assert "! models.Money.amount: type int -> float" in out
    assert "Semver impact: major" in out


def test_surface_diff_rules() -> None:
    old = {
        "methods": {
            "a.get": {
                "operation": "GET /a",
                "params": {"x": {"type": "str", "required": False}},
                "returns": "A",
                "deprecated": False,
            }
        },
        "models": {
            "A": {"bases": [], "fields": {"f": {"type": "int", "required": True}}}
        },
    }
    new = {
        "methods": {
            "a.get": {
                "operation": "GET /a",
                "params": {
                    "x": {"type": "str", "required": False},
                    "y": {"type": "int", "required": False},
                },
                "returns": "A",
                "deprecated": True,
            },
            "b.list": {
                "operation": "GET /b",
                "params": {},
                "returns": "B",
                "deprecated": False,
            },
        },
        "models": {
            "A": {
                "bases": [],
                "fields": {
                    "f": {"type": "int", "required": True},
                    "g": {"type": "str", "required": False},
                },
            }
        },
    }
    diff = diff_surfaces(old, new)
    assert diff.bump == "minor" and not diff.breaking
    assert diff_surfaces(old, old).bump == "none"
    assert diff_surfaces(new, old).bump == "major"


@pytest.mark.parametrize(
    ("version", "bump", "expected"),
    [
        ("0.3.1", "major", "0.4.0"),
        ("0.3.1", "minor", "0.3.2"),
        ("1.2.3", "major", "2.0.0"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "none", "1.2.3"),
    ],
)
def test_suggest_version(version: str, bump: str, expected: str) -> None:
    assert suggest_version(version, bump) == expected  # type: ignore[arg-type]


def test_score_and_ir_commands(
    specs: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["score", str(specs / "acme.yaml"), "--format", "json"]) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["paginated_methods"] == 4 and card["streaming_methods"] == 2
    assert card["methods_with_docstrings"] <= card["methods"]
    assert main(["ir", str(specs / "nullable_30.yaml")]) == 0
    ir = json.loads(capsys.readouterr().out)
    assert ir["openapi_version"] == "3.1.0"
    assert (
        main(["ir", str(specs / "petstore_swagger2.json"), "--stage", "normalized"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["openapi"] == "3.1.0"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.startswith("apismith ")

"""The ``spec2py`` command line.

::

    spec2py generate openapi.yaml -o clients/acme
    spec2py fastapi myapp.main:app -o clients/myapp
    spec2py lint openapi.yaml --fix-overlay fixes.yaml
    spec2py update            # re-pull, diff the API, suggest a version
    spec2py update --check    # CI: fail if the committed client is stale
    spec2py diff old.yaml new.yaml
    spec2py score openapi.yaml
"""

import argparse
import json
import pathlib
import sys
import tempfile
from collections.abc import Sequence

import yaml

from . import __version__
from .config import Config, ConfigError, load_config
from .emit.package import MANIFEST, package_dir, read_manifest
from .ir import to_jsonable
from .lint import lint
from .loader import LoadError
from .normalize import NormalizeError, RefError
from .overlay import OverlayError
from .pipeline import Pipeline, generate, load_api, load_ir, write_pipeline
from .score import scorecard
from .surface import api_surface, diff_surfaces, suggest_version

__all__ = ["main"]


class CLIError(Exception):
    pass


def _config(args: argparse.Namespace) -> Config:
    config = load_config(
        pathlib.Path(args.config) if getattr(args, "config", None) else None
    )
    overrides: dict[str, object] = {}
    for key in (
        "package",
        "client_name",
        "base_url",
        "layout",
        "group_by",
        "package_version",
        "project_name",
    ):
        value = getattr(args, key, None)
        if value is not None:
            overrides[key] = value
    if getattr(args, "overlay", None):
        overrides["overlays"] = (*config.overlays, *args.overlay)
    if getattr(args, "output", None):
        overrides["output"] = pathlib.Path(args.output)
    return config.replace(**overrides) if overrides else config


def _spec(args: argparse.Namespace, config: Config) -> str:
    spec = getattr(args, "spec", None) or config.spec
    if not spec:
        raise CLIError("no spec given: pass one or set `spec` in [tool.spec2py]")
    return str(spec)


def _add_generation_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-o",
        "--output",
        help="output directory (default: [tool.spec2py] output, or ./client)",
    )
    p.add_argument(
        "--package", help="Python package name (default: from the API title)"
    )
    p.add_argument(
        "--client-name", help="client class name (default: from the API title)"
    )
    p.add_argument(
        "--base-url", help="default base URL (default: the spec's first server)"
    )
    p.add_argument(
        "--layout",
        choices=["project", "package"],
        help="'project' adds pyproject.toml + src/ (default); 'package' writes only the package",
    )
    p.add_argument(
        "--group-by",
        choices=["tag", "path"],
        help="how operations group into resources",
    )
    p.add_argument(
        "--package-version", help="version written to the package's _version.py"
    )
    p.add_argument(
        "--project-name", help="distribution name in the scaffolded pyproject.toml"
    )
    p.add_argument(
        "--overlay", action="append", help="Overlay document to apply (repeatable)"
    )
    p.add_argument("--config", help="pyproject.toml to read [tool.spec2py] from")


def cmd_generate(args: argparse.Namespace) -> int:
    config = _config(args)
    spec = _spec(args, config)
    if args.check:
        return _check(load_api(spec, config))
    generated = generate(spec, config.output, config)
    print(
        f"Generated {generated.package} ({len(generated.files)} files) in {config.output}"
    )
    if args.verbose:
        for rel in sorted(generated.files):
            print(f"  {rel}")
    return 0


def cmd_fastapi(args: argparse.Namespace) -> int:
    args.spec = args.app
    args.check = False
    return cmd_generate(args)


def _check(pipeline: Pipeline) -> int:
    """Regenerate into a temp dir and compare with what's on disk."""
    config = pipeline.config
    current = package_dir(pipeline.plan, config.output)
    with tempfile.TemporaryDirectory(prefix="spec2py-check-") as tmp:
        fresh_root = pathlib.Path(tmp)
        write_pipeline(pipeline, fresh_root)
        fresh = package_dir(pipeline.plan, fresh_root)
        stale: list[str] = []
        for path in sorted(fresh.rglob("*")):
            relative = path.relative_to(fresh)
            if (
                path.is_dir()
                or relative.parts[0] == "_custom"
                or relative.name == MANIFEST
            ):
                continue
            existing = current / relative
            if not existing.is_file() or existing.read_bytes() != path.read_bytes():
                stale.append(str(relative))
        old = read_manifest(current).get("files")
        if isinstance(old, dict):
            stale.extend(
                sorted(
                    f"{rel} (should be removed)"
                    for rel in old
                    if not (fresh / rel).exists()
                )
            )
    if stale:
        print(f"Client in {current} is out of date with the spec:", file=sys.stderr)
        for rel in stale:
            print(f"  {rel}", file=sys.stderr)
        print(
            "Run `spec2py update` (or `spec2py generate`) to regenerate.",
            file=sys.stderr,
        )
        return 1
    print(f"Client in {current} is up to date.")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    config = _config(args)
    spec = _spec(args, config)
    pipeline = load_api(spec, config)
    target = package_dir(pipeline.plan, config.output)
    manifest = read_manifest(target)
    old_surface = manifest.get("surface")
    new_surface = api_surface(pipeline.plan)
    current_version = str(manifest.get("package_version") or config.package_version)
    if isinstance(old_surface, dict):
        diff = diff_surfaces(old_surface, new_surface)
        print(diff.render())
        if diff.bump != "none":
            print(
                f"Suggested version: {current_version} -> {suggest_version(current_version, diff.bump)} ({diff.bump})"
            )
    else:
        print("No previous generation found; generating from scratch.")
    if args.check:
        return _check(pipeline)
    write_pipeline(pipeline)
    print(f"Regenerated {pipeline.plan.client.package} in {config.output}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    config = _config(args)
    _, api, _ = load_ir(_spec(args, config), config)
    report = lint(api)
    print(
        report.to_json()
        if args.format == "json"
        else report.render(verbose=args.verbose)
    )
    if args.fix_overlay:
        overlay = report.overlay(api.title)
        pathlib.Path(args.fix_overlay).write_text(
            yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8"
        )
        print(f"\nWrote {len(overlay['actions'])} fix(es) to {args.fix_overlay}")
    return 1 if args.strict and report.findings else 0


def cmd_diff(args: argparse.Namespace) -> int:
    config = _config(args)
    old = api_surface(load_api(args.old, config).plan)
    new = api_surface(load_api(args.new, config).plan)
    diff = diff_surfaces(old, new)
    print(diff.render())
    if diff.bump != "none":
        print(f"Semver impact: {diff.bump}")
    return 1 if args.fail_on_breaking and diff.breaking else 0


def cmd_ir(args: argparse.Namespace) -> int:
    config = _config(args)
    document, api, _ = load_ir(_spec(args, config), config)
    data = to_jsonable(api) if args.stage == "ir" else document
    print(json.dumps(data, indent=2, sort_keys=args.stage != "ir"))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    config = _config(args)
    card = scorecard(_spec(args, config), config, typecheck=args.typecheck)
    print(json.dumps(card, indent=2) if args.format == "json" else card_text(card))
    return 0


def card_text(card: dict[str, object]) -> str:
    width = max(len(k) for k in card)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in card.items())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spec2py",
        description="Generate first-class Python clients from OpenAPI specs.",
        suggest_on_error=True,
    )
    parser.add_argument("--version", action="version", version=f"spec2py {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="generate a client package")
    p.add_argument("spec", nargs="?", help="path, URL or FastAPI app (module:app)")
    _add_generation_options(p)
    p.add_argument(
        "--check",
        action="store_true",
        help="don't write; fail if the client on disk is stale",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser(
        "fastapi", help="generate a client from a FastAPI app (module:app)"
    )
    p.add_argument("app", help="import path of the app, e.g. myapp.main:app")
    _add_generation_options(p)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_fastapi)

    p = sub.add_parser(
        "update", help="regenerate, print the API diff and suggest a version"
    )
    p.add_argument("spec", nargs="?")
    _add_generation_options(p)
    p.add_argument(
        "--check",
        action="store_true",
        help="CI mode: fail if the committed client is stale",
    )
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("lint", help="explain what in a spec will generate poorly")
    p.add_argument("spec", nargs="?")
    p.add_argument("--overlay", action="append")
    p.add_argument("--config")
    p.add_argument(
        "--fix-overlay",
        metavar="FILE",
        help="write a starter Overlay fixing mechanical issues",
    )
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument(
        "--strict", action="store_true", help="exit 1 if there are any findings"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("diff", help="compare the client API of two specs")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--config")
    p.add_argument("--fail-on-breaking", action="store_true")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser(
        "score", help="quality scorecard for the client a spec generates"
    )
    p.add_argument("spec", nargs="?")
    p.add_argument("--config")
    p.add_argument("--overlay", action="append")
    p.add_argument(
        "--typecheck", action="store_true", help="also run mypy --strict on the output"
    )
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser(
        "ir", help="print the normalized spec or the IR as JSON (debugging)"
    )
    p.add_argument("spec", nargs="?")
    p.add_argument("--config")
    p.add_argument("--overlay", action="append")
    p.add_argument("--stage", choices=["normalized", "ir"], default="ir")
    p.set_defaults(func=cmd_ir)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        CLIError,
        ConfigError,
        LoadError,
        NormalizeError,
        OverlayError,
        RefError,
    ) as exc:
        print(f"spec2py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

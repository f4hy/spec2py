"""Run the generator over the real-world spec corpus and print the scorecard.

    uv run corpus/run.py                 # all specs (skip large ones with --small)
    uv run corpus/run.py stripe github   # just these
    uv run corpus/run.py --gates         # also run mypy/pyright/ty/ruff on each client
    uv run corpus/run.py --repin         # accept upstream changes and update hashes

Exit status is non-zero if any spec fails to generate or any gate fails.
"""

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any

import httpx2

from apismith.config import Config
from apismith.score import scorecard

HERE = pathlib.Path(__file__).parent
SPECS = HERE / "specs"


def fetch(entry: dict[str, Any], *, repin: bool) -> pathlib.Path:
    suffix = ".yaml" if entry["url"].endswith((".yaml", ".yml")) else ".json"
    path = SPECS / f"{entry['name']}{suffix}"
    if not path.exists():
        SPECS.mkdir(exist_ok=True)
        response = httpx2.get(entry["url"], follow_redirects=True, timeout=120)
        response.raise_for_status()
        path.write_bytes(response.content)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        if not repin:
            raise RuntimeError(
                f"{entry['name']}: hash changed upstream ({digest}); rerun with --repin"
            )
        entry["sha256"] = digest
    return path


def gates(package: pathlib.Path) -> dict[str, str]:
    (package.parent / "pyrightconfig.json").write_text(
        json.dumps({"typeCheckingMode": "strict", "pythonVersion": "3.14"})
    )
    bin_dir = pathlib.Path(sys.executable).parent
    commands = {
        "ruff": [
            str(bin_dir / "ruff"),
            "check",
            "--isolated",
            "--target-version",
            "py314",
            package.name,
        ],
        "mypy": [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            package.name,
        ],
        "pyright": [str(bin_dir / "pyright"), package.name],
        "ty": [str(bin_dir / "ty"), "check", package.name],
    }
    results: dict[str, str] = {}
    for name, command in commands.items():
        proc = subprocess.run(
            command, cwd=package.parent, capture_output=True, text=True, check=False
        )
        results[name] = "pass" if proc.returncode == 0 else "FAIL"
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("names", nargs="*")
    parser.add_argument("--gates", action="store_true")
    parser.add_argument("--small", action="store_true", help="skip specs marked large")
    parser.add_argument("--repin", action="store_true")
    parser.add_argument(
        "--json", action="store_true", help="print the scorecard as JSON"
    )
    args = parser.parse_args()

    manifest = tomllib.loads((HERE / "manifest.toml").read_text())
    entries = [
        e
        for e in manifest["spec"]
        if (not args.names or e["name"] in args.names)
        and not (args.small and e.get("large"))
    ]
    rows: list[dict[str, Any]] = []
    failed = False
    for entry in entries:
        start = time.perf_counter()
        try:
            path = fetch(entry, repin=args.repin)
            card = scorecard(str(path), Config(layout="package"))
            if args.gates:
                with tempfile.TemporaryDirectory() as tmp:
                    from apismith import generate

                    generated = generate(str(path), tmp, Config(layout="package"))
                    card.update(gates(pathlib.Path(tmp) / generated.package))
                    failed |= "FAIL" in card.values()
            card["seconds"] = round(time.perf_counter() - start, 1)
            rows.append({"name": entry["name"], **card})
        except Exception as exc:  # report and keep going
            failed = True
            rows.append(
                {"name": entry["name"], "error": f"{type(exc).__name__}: {exc}"}
            )
        print(json.dumps(rows[-1]) if args.json else _row(rows[-1]), flush=True)
    if args.repin:
        _write_manifest(manifest)
    return 1 if failed else 0


def _row(row: dict[str, Any]) -> str:
    if "error" in row:
        return f"{row['name']:<14} ERROR {row['error']}"
    keys = [
        "operations",
        "methods",
        "models",
        "lines_of_code",
        "methods_with_docstrings",
        "paginated_methods",
        "streaming_methods",
        "approximations",
    ]
    gates_ = " ".join(
        f"{k}={row[k]}" for k in ("ruff", "mypy", "pyright", "ty") if k in row
    )
    labels = {
        "lines_of_code": "loc",
        "methods_with_docstrings": "documented",
        "paginated_methods": "paginated",
        "streaming_methods": "streaming",
    }
    stats = " ".join(f"{labels.get(k, k)}={row[k]}" for k in keys)
    return f"{row['name']:<14} {stats} {gates_} ({row['seconds']}s)"


def _write_manifest(manifest: dict[str, Any]) -> None:
    lines = (HERE / "manifest.toml").read_text().split("[[spec]]")[0].rstrip() + "\n"
    for e in manifest["spec"]:
        lines += f'\n[[spec]]\nname = "{e["name"]}"\nurl = "{e["url"]}"\nsha256 = "{e["sha256"]}"\n'
        if e.get("large"):
            lines += "large = true\n"
    (HERE / "manifest.toml").write_text(lines)


if __name__ == "__main__":
    shutil.rmtree(HERE / "__pycache__", ignore_errors=True)
    raise SystemExit(main())

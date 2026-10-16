"""Quality scorecard: measurable properties of the client a spec generates.

Published per corpus spec, this is the project's public benchmark against other
generators: size, documentation coverage, detected pagination and streaming,
lossy mappings, and (optionally) whether the output passes ``mypy --strict``.
"""

import pathlib
import subprocess
import sys
import tempfile
from typing import Any

from .config import Config
from .emit.package import package_dir
from .pipeline import generate, load_api

__all__ = ["scorecard"]


def scorecard(source: str, config: Config, *, typecheck: bool = False) -> dict[str, Any]:
    pipeline = load_api(source, config)
    plan = pipeline.plan
    methods = [m for r in plan.resources for m in r.methods]
    with tempfile.TemporaryDirectory(prefix="apismith-score-") as tmp:
        out = pathlib.Path(tmp)
        generated = generate(source, out, config.replace(layout="package"))
        py_files = {k: v for k, v in generated.files.items() if k.endswith(".py")}
        loc = sum(
            1
            for text in py_files.values()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        card: dict[str, Any] = {
            "spec": f"{plan.client.title} {plan.client.version}",
            "operations": len(pipeline.api.operations),
            "methods": len(methods),
            "resources": len(plan.resources),
            "models": len(plan.models),
            "files": len(py_files),
            "lines_of_code": loc,
            "methods_with_docstrings": sum(1 for m in methods if m.summary or m.description),
            "paginated_methods": sum(1 for m in methods if m.pagination),
            "streaming_methods": sum(1 for m in methods if m.stream),
            "approximations": len(plan.notes),
        }
        if typecheck:
            target = package_dir(plan, out)
            result = subprocess.run(
                [sys.executable, "-m", "mypy", "--strict", "--no-incremental", target.name],
                cwd=target.parent,
                capture_output=True,
                text=True,
                check=False,
            )
            card["mypy_strict"] = "pass" if result.returncode == 0 else result.stdout.strip().splitlines()[-1]
    return card

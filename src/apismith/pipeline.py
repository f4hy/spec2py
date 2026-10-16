"""Glue the five stages together: load, normalize, IR, plan, emit."""

import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from .build_ir import build_ir
from .config import Config
from .emit.package import GeneratedPackage, write_package
from .ir import API
from .loader import load_spec
from .normalize import normalize
from .overlay import OverlayReport
from .plan import Plan, plan_api

__all__ = ["Pipeline", "generate", "load_api"]


@dataclass(slots=True)
class Pipeline:
    """The intermediate products of one run, kept for lint, diff and tests."""

    config: Config
    document: dict[str, Any]
    api: API
    plan: Plan
    overlay_reports: list[OverlayReport] = field(default_factory=list[OverlayReport])

    @property
    def spec_digest(self) -> str:
        canonical = json.dumps(self.document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def load_api(source: str, config: Config | None = None) -> Pipeline:
    """Run load -> normalize -> IR -> plan for ``source``."""
    config = config or Config()
    raw, base, reports = load_spec(source, config.overlays)
    document = normalize(raw, base)
    api = build_ir(document)
    plan = plan_api(api, config)
    return Pipeline(config=config, document=document, api=api, plan=plan, overlay_reports=reports)


def generate(
    source: str,
    output: pathlib.Path | str | None = None,
    config: Config | None = None,
) -> GeneratedPackage:
    """Generate a client package for ``source`` into ``output``."""
    config = config or Config()
    pipeline = load_api(source, config)
    out = pathlib.Path(output) if output is not None else config.output
    from .surface import api_surface

    return write_package(
        pipeline.plan,
        out,
        manifest_extra={
            "spec_sha256": pipeline.spec_digest,
            "package_version": config.package_version,
            "surface": api_surface(pipeline.plan),
        },
    )

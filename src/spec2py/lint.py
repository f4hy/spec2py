"""Explain what in a spec will generate poorly, ranked by impact on the output.

Every finding says what the generator will do about it. Mechanical problems
(missing operationIds, an obvious discriminator) can be fixed by a starter
Overlay that ``spec2py lint --fix-overlay`` writes.
"""

import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from .build_ir import media_rank
from .ir import API, Kind, Operation, Schema
from .naming import pascal, singular

__all__ = ["Finding", "LintReport", "lint"]

type Severity = Literal["error", "warning", "info"]


@dataclass(slots=True)
class Finding:
    code: str
    severity: Severity
    message: str
    """One line, with the affected count."""
    consequence: str
    locations: list[str] = field(default_factory=list[str])
    fix: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    """Overlay actions that fix it, when the fix is mechanical."""

    @property
    def impact(self) -> int:
        weight = {"error": 100, "warning": 10, "info": 1}[self.severity]
        return weight * max(len(self.locations), 1)


@dataclass(slots=True)
class LintReport:
    findings: list[Finding]

    def ranked(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (-f.impact, f.code))

    def render(self, *, verbose: bool = False) -> str:
        if not self.findings:
            return "No issues found: this spec should generate cleanly."
        lines: list[str] = []
        for f in self.ranked():
            lines.append(f"[{f.severity}] {f.code}: {f.message}")
            lines.append(f"    -> {f.consequence}")
            shown = f.locations if verbose else f.locations[:5]
            lines.extend(f"       {loc}" for loc in shown)
            if len(f.locations) > len(shown):
                lines.append(
                    f"       ... and {len(f.locations) - len(shown)} more (--verbose)"
                )
        fixable = sum(1 for f in self.findings if f.fix)
        if fixable:
            lines.append(
                f"\n{fixable} finding(s) can be fixed mechanically: spec2py lint --fix-overlay fixes.yaml"
            )
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    "consequence": f.consequence,
                    "locations": f.locations,
                }
                for f in self.ranked()
            ],
            indent=2,
        )

    def overlay(self, title: str) -> dict[str, Any]:
        actions = [a for f in self.ranked() for a in f.fix]
        return {
            "overlay": "1.0.0",
            "info": {"title": f"spec2py fixes for {title}", "version": "1.0.0"},
            "actions": actions,
        }

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]


def _jsonpath_key(key: str) -> str:
    return "'" + key.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _derived_operation_id(op: Operation) -> str:
    statics = [s for s in op.path.split("/") if s and not s.startswith("{")]
    is_item = op.path.rstrip("/").endswith("}")
    verb = {
        ("get", False): "list",
        ("get", True): "get",
        ("post", False): "create",
        ("put", True): "update",
        ("patch", True): "update",
        ("delete", True): "delete",
    }.get((op.method, is_item), op.method)
    nouns = statics[-1:] if statics else ["root"]
    noun = pascal(nouns[0])
    if verb in ("get", "update", "delete", "create"):
        noun = pascal(singular(nouns[0]))
    return verb + noun


def _schemas_in(api: API) -> Iterator[tuple[str, Schema]]:
    def walk(where: str, s: Schema | None) -> Iterator[tuple[str, Schema]]:
        if s is None:
            return
        yield where, s
        for p in s.properties:
            yield from walk(f"{where}.{p.name}", p.schema)
        yield from walk(f"{where}[]", s.items)
        yield from walk(f"{where}{{}}", s.additional)
        for i, v in enumerate(s.variants):
            yield from walk(f"{where}|{i}", v)

    for name, s in api.schemas.items():
        yield from walk(f"#/components/schemas/{name}", s)
    for op in api.operations:
        if op.request_body:
            yield from walk(f"{op.key} requestBody", op.request_body.schema)
        for r in op.responses:
            yield from walk(f"{op.key} {r.status}", r.schema)


def _common_tag(api: API, s: Schema) -> str | None:
    """A property every variant pins to one distinct const/enum value."""
    candidates: dict[str, set[object]] = defaultdict(set)
    for v in s.variants:
        target = api.resolve(v)
        if target.kind is not Kind.OBJECT:
            return None
        for p in target.properties:
            ps = api.resolve(p.schema)
            if ps.kind in (Kind.CONST, Kind.ENUM) and len(ps.values) == 1:
                candidates[p.name].add(ps.values[0])
    for name, values in sorted(candidates.items()):
        if len(values) == len(s.variants):
            return name
    return None


def lint(api: API) -> LintReport:
    findings: dict[str, Finding] = {}

    def add(
        code: str,
        severity: Severity,
        message: str,
        consequence: str,
        location: str,
        fix: dict[str, Any] | None = None,
    ) -> None:
        f = findings.setdefault(code, Finding(code, severity, message, consequence))
        if location not in f.locations:
            f.locations.append(location)
        if fix is not None:
            f.fix.append(fix)

    seen_ids: dict[str, str] = {}
    for op in api.operations:
        target = f"$.paths[{_jsonpath_key(op.path)}].{op.method}"
        if not op.operation_id:
            add(
                "missing-operation-id",
                "warning",
                "operations have no operationId",
                "method names are derived from the HTTP method and path",
                op.key,
                {
                    "target": target,
                    "update": {"operationId": _derived_operation_id(op)},
                },
            )
        elif op.operation_id in seen_ids:
            add(
                "duplicate-operation-id",
                "error",
                "operationIds are not unique",
                "later duplicates get a numeric suffix",
                f"{op.key} (also {seen_ids[op.operation_id]})",
            )
        else:
            seen_ids[op.operation_id] = op.key
        if not op.tags:
            add(
                "missing-tags",
                "info",
                "operations have no tags",
                "they are grouped into resources by their first path segment",
                op.key,
            )
        success = [r for r in op.responses if r.is_success]
        if not success:
            add(
                "no-success-response",
                "warning",
                "operations declare no 2xx response",
                "their methods return None",
                op.key,
            )
        elif any(r.media_type for r in success) and all(
            r.schema is None and r.media_type and media_rank(r.media_type) <= 1
            for r in success
            if r.media_type
        ):
            add(
                "untyped-response",
                "warning",
                "JSON responses have no schema",
                "their methods return `object`",
                op.key,
            )
        if not any(not r.is_success for r in op.responses):
            add(
                "no-error-responses",
                "info",
                "operations declare no error responses",
                "errors raise typed exceptions, but bodies are not parsed into models",
                op.key,
            )
        if (
            op.request_body
            and media_rank(op.request_body.media_type) <= 1
            and op.request_body.schema.kind is Kind.ANY
        ):
            add(
                "untyped-request-body",
                "warning",
                "request bodies have no schema",
                "the body is accepted as `object` without validation",
                op.key,
            )
        for p in op.parameters:
            if p.schema.lossy:
                add(
                    "content-parameter",
                    "info",
                    "parameters use `content` instead of `schema`",
                    "they are sent as plain text",
                    f"{op.key} {p.name}",
                )

    for where, s in _schemas_in(api):
        if (
            s.kind is Kind.UNION
            and s.union_kind in ("oneOf", "anyOf")
            and s.discriminator is None
        ):
            models = [v for v in s.variants if api.resolve(v).kind is Kind.OBJECT]
            if len(models) > 1:
                tag = _common_tag(api, s)
                fix = None
                if (
                    tag
                    and where.startswith("#/components/schemas/")
                    and "." not in where
                    and "|" not in where
                ):
                    name = where.rsplit("/", 1)[-1]
                    fix = {
                        "target": f"$.components.schemas[{_jsonpath_key(name)}]",
                        "update": {"discriminator": {"propertyName": tag}},
                    }
                add(
                    "union-without-discriminator",
                    "warning",
                    "unions of objects have no discriminator",
                    "pydantic tries each member in order (slower, and ambiguous payloads may pick the wrong model)",
                    where + (f" (could discriminate on `{tag}`)" if tag else ""),
                    fix,
                )
        for lossy in s.lossy:
            code = "untyped-schema" if lossy == "untyped schema" else "lossy-mapping"
            consequence = (
                "typed as `object`; add a schema with an Overlay"
                if code == "untyped-schema"
                else lossy
            )
            add(
                code,
                "info",
                "schemas map to Python lossily"
                if code == "lossy-mapping"
                else "schemas have no type",
                consequence,
                where,
            )
    inline_enums: dict[tuple[object, ...], list[str]] = defaultdict(list)
    for where, s in _schemas_in(api):
        if s.kind is Kind.ENUM and not (
            where.startswith("#/components/schemas/") and "." not in where
        ):
            inline_enums[tuple(s.values)].append(where)
    for values, wheres in inline_enums.items():
        if len(wheres) > 1:
            add(
                "repeated-inline-enum",
                "info",
                "identical inline enums are repeated",
                "each gets its own type alias; a shared component would give one name",
                f"{list(values)!r}: {', '.join(wheres[:3])}",
            )

    return LintReport(list(findings.values()))

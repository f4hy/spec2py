"""The public API surface of a generated client, diffs between two surfaces, and semver advice.

The surface is recorded in the generated package's manifest, so
``apismith update`` can say exactly what changed since the last generation and
whether that is breaking.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .plan import MISSING, PModel, Plan

__all__ = ["Change", "SurfaceDiff", "api_surface", "diff_surfaces", "suggest_version"]

type Surface = dict[str, Any]
type Bump = Literal["major", "minor", "patch", "none"]


def api_surface(plan: Plan) -> Surface:
    """A JSON-able summary of every method signature and model field."""
    methods: dict[str, Any] = {}
    for r in plan.resources:
        for m in r.methods:
            returns = m.return_type.render()
            if m.pagination:
                returns = f"Page[{m.pagination.item_type.render()}]"
            elif m.stream:
                returns = f"Stream[{m.stream.event_type.render()}]"
            methods[f"{r.attr}.{m.name}"] = {
                "operation": m.operation.key,
                "params": {
                    p.py_name: {"type": p.type.render(), "required": p.required}
                    for p in m.params
                },
                "returns": returns,
                "deprecated": m.deprecated,
            }
    models: dict[str, Any] = {}
    for decl in plan.models:
        if isinstance(decl, PModel):
            models[decl.name] = {
                "bases": decl.bases,
                "fields": {
                    f.py_name: {
                        "type": f.type.render(),
                        "required": f.required and f.default is MISSING,
                    }
                    for f in decl.fields
                },
            }
        else:
            models[decl.name] = {"alias": decl.type.render()}
    return {"methods": methods, "models": models}


@dataclass(frozen=True, slots=True)
class Change:
    kind: Literal["added", "removed", "changed"]
    path: str
    detail: str
    breaking: bool


@dataclass(slots=True)
class SurfaceDiff:
    changes: list[Change] = field(default_factory=list[Change])

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.breaking]

    @property
    def bump(self) -> Bump:
        if not self.changes:
            return "none"
        if self.breaking:
            return "major"
        if any(c.kind == "added" for c in self.changes):
            return "minor"
        return "patch"

    def render(self) -> str:
        if not self.changes:
            return "No API changes."
        lines = []
        for c in sorted(self.changes, key=lambda c: (not c.breaking, c.path)):
            marker = "!" if c.breaking else {"added": "+", "removed": "-", "changed": "~"}[c.kind]
            lines.append(f"  {marker} {c.path}: {c.detail}")
        summary = f"{len(self.changes)} change(s), {len(self.breaking)} breaking"
        return summary + "\n" + "\n".join(lines)


def _diff_params(path: str, old: dict[str, Any], new: dict[str, Any], out: list[Change]) -> None:
    for name in sorted(old.keys() - new.keys()):
        out.append(Change("removed", f"{path}({name})", "parameter removed", True))
    for name in sorted(new.keys() - old.keys()):
        required = new[name]["required"]
        out.append(Change("added", f"{path}({name})", "required parameter added" if required else "optional parameter added", required))
    for name in sorted(old.keys() & new.keys()):
        o, n = old[name], new[name]
        if o["type"] != n["type"]:
            out.append(Change("changed", f"{path}({name})", f"type {o['type']} -> {n['type']}", True))
        elif n["required"] and not o["required"]:
            out.append(Change("changed", f"{path}({name})", "now required", True))
        elif o["required"] and not n["required"]:
            out.append(Change("changed", f"{path}({name})", "now optional", False))


def diff_surfaces(old: Surface, new: Surface) -> SurfaceDiff:
    """Compare two surfaces. Removals and narrowing changes are breaking."""
    changes: list[Change] = []
    old_m = cast(dict[str, Any], old.get("methods", {}))
    new_m = cast(dict[str, Any], new.get("methods", {}))
    for name in sorted(old_m.keys() - new_m.keys()):
        changes.append(Change("removed", name, f"method removed ({old_m[name]['operation']})", True))
    for name in sorted(new_m.keys() - old_m.keys()):
        changes.append(Change("added", name, f"method added ({new_m[name]['operation']})", False))
    for name in sorted(old_m.keys() & new_m.keys()):
        o, n = old_m[name], new_m[name]
        _diff_params(name, o["params"], n["params"], changes)
        if o["returns"] != n["returns"]:
            changes.append(Change("changed", name, f"returns {o['returns']} -> {n['returns']}", True))
        if n["deprecated"] and not o["deprecated"]:
            changes.append(Change("changed", name, "deprecated", False))
    old_t = cast(dict[str, Any], old.get("models", {}))
    new_t = cast(dict[str, Any], new.get("models", {}))
    for name in sorted(old_t.keys() - new_t.keys()):
        changes.append(Change("removed", f"models.{name}", "model removed", True))
    for name in sorted(new_t.keys() - old_t.keys()):
        changes.append(Change("added", f"models.{name}", "model added", False))
    for name in sorted(old_t.keys() & new_t.keys()):
        o, n = old_t[name], new_t[name]
        if "alias" in o or "alias" in n:
            if o.get("alias") != n.get("alias"):
                changes.append(Change("changed", f"models.{name}", f"{o.get('alias')} -> {n.get('alias')}", True))
            continue
        of, nf = o["fields"], n["fields"]
        for f in sorted(of.keys() - nf.keys()):
            changes.append(Change("removed", f"models.{name}.{f}", "field removed", True))
        for f in sorted(nf.keys() - of.keys()):
            changes.append(Change("added", f"models.{name}.{f}", "field added", nf[f]["required"]))
        for f in sorted(of.keys() & nf.keys()):
            if of[f]["type"] != nf[f]["type"]:
                changes.append(Change("changed", f"models.{name}.{f}", f"type {of[f]['type']} -> {nf[f]['type']}", True))
    return SurfaceDiff(changes)


def suggest_version(current: str, bump: Bump) -> str:
    """Next version for ``bump``. Before 1.0, breaking changes bump the minor
    and additions bump the patch (the Cargo convention)."""
    parts = [int(p) if p.isdigit() else 0 for p in (current.split(".") + ["0", "0"])[:3]]
    major, minor, patch = parts
    match bump:
        case "major" if major == 0:
            return f"0.{minor + 1}.0"
        case "major":
            return f"{major + 1}.0.0"
        case "minor" if major == 0:
            # 0.x: the minor number signals breaks, so features bump the patch.
            return f"0.{minor}.{patch + 1}"
        case "minor":
            return f"{major}.{minor + 1}.0"
        case "patch":
            return f"{major}.{minor}.{patch + 1}"
        case _:
            return current

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`spec2py` is a Python-only OpenAPI client generator (the design doc is
"A First-Class OpenAPI Client Generator for Python"). It's a uv workspace with
two distributions:

- `spec2py` (repo root, `src/spec2py/`): the generator and CLI.
- `spec2py-runtime` (`runtime/`, `runtime/src/spec2py_runtime/`): what
  generated clients import at runtime. It depends only on httpx2, pydantic and
  anyio. Request behaviour (retries, auth, pagination, streaming, errors) belongs
  here, never in generated code.

Python 3.14 only. Use modern syntax freely: PEP 695 generics and `type` aliases,
PEP 649 lazy annotations (no `from __future__ import annotations`), PEP 758
`except A, B:`, `warnings.deprecated`.

## Commands

```bash
uv sync --all-packages
uv run pytest -q                                  # everything (~230 tests, ~25s)
uv run pytest -q -m "not slow"                    # skip static gates on generated code
uv run pytest tests/test_plan.py::test_pagination_detection -q
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pyright
SPEC2PY_UPDATE_GOLDEN=1 uv run pytest tests/test_emit.py tests/test_normalize_ir.py   # accept output changes
uv run corpus/run.py --gates                      # real-world specs; --small skips Stripe/GitHub
uv run spec2py generate tests/specs/acme.yaml -o /tmp/acme --layout package
```

mypy is strict everywhere. pyright is strict for `runtime/src` and standard for
the generator.

## Architecture: a five-stage compiler

`pipeline.py` wires the stages together. Each stage only reads the previous one's output.

1. **Load** (`loader.py`, `overlay.py`, `jsonpath.py`, `config.py`): a file, URL or
   `module:app` FastAPI import. Overlays use our own RFC 9535 JSONPath, which
   returns mutable locations.
2. **Normalize** (`normalize/`): `swagger2.py` (2.0 → 3.0), `refs.py` (bundles
   external `$ref`s into `#/components/schemas`), `upgrade.py` (3.0 → 3.1
   semantics: `nullable` becomes a `type` array or an `anyOf` with null).
3. **IR** (`ir.py`, `build_ir.py`): a `Schema` node per construct (`Kind` enum),
   plus `Operation`, `Response` and `SecurityScheme`. Refs stay as
   `Kind.REF(name)`, so there are no cycles. `spec2py ir` prints it, and
   `tests/golden/acme.ir.json` snapshots it.
4. **Plan** (`plan.py`, `naming.py`): every Python decision. That covers
   model/field/method names, `type_of()` (the type mapping), resource grouping,
   flat bodies (fields from the body model, validated by
   `spec2py_runtime.build_body`), pagination detection (`detect_pagination`),
   streaming (`stream_format`, `make_stream`), auth params, and `notes` (which
   become NOTES.md).
5. **Emit** (`emit/`): builds `ast.Module`s. `pyast.py` has the `TypeExpr` tree
   and `Imports` (clash-safe imports, `shadowed` builtins, `TYPE_CHECKING`
   deferral). Resources and the client are emitted as async code, and
   `unasync.py` derives the sync class. `package.py` runs ruff (isort + format),
   writes files, keeps `_custom/`, removes stale files and writes
   `.spec2py-manifest.json` (file hashes and the API surface `update` diffs
   against).

Rules learned from the corpus. Keep them:

- Never narrow an inherited field. Discriminator tags and type changes make the
  model copy its base's fields instead (`declare_object`), because ty and mypy
  reject it.
- A field or parameter named like a builtin (`object`, `type`, `list`) shadows it
  in annotations (class scope, PEP 649) or in method bodies. Use
  `Imports.shadowed` to render `builtins.x`.
- Over `SPLIT_THRESHOLD` models, `models` becomes a package (`_bases`,
  `_aliases`, name-family chunks), because pyright gives up on huge modules.
  Runtime imports must stay acyclic. `link_models()` plus `defer_build=True`
  keep imports fast.
- `cast(type[X], X)` is used for `cast_to` with non-class types; strip
  `Annotated` in `type[...]`.

## Tests

- `tests/runtime/`: runtime behaviour against fault-injecting `MockTransport`s.
- `tests/test_generated_client.py`: the generated Acme client against a mock server.
- `tests/test_fastapi_roundtrip.py`: `spec2py fastapi` on `tests/fastapi_app/app.py`, then calls through `ASGITransport`.
- `tests/test_static_gates.py` (marked `slow`): mypy, pyright, ty and ruff on generated clients.
- `tests/test_emit.py`: golden snapshots in `tests/golden/`, determinism, regeneration safety.
- `tests/test_properties.py`: Hypothesis round trips of random schemas and payloads.

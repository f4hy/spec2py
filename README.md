# spec2py

A Python-only OpenAPI client generator aimed at the quality of the best
commercial SDKs. You get Pydantic v2 models, an [httpx2](https://github.com/pydantic/httpx2)
runtime, sync and async clients generated from one source, and pagination,
retries, streaming and uploads built in.

```console
$ uvx spec2py generate https://api.acme.com/openapi.json -o clients/acme
$ uvx --with fastapi spec2py fastapi myapp.main:app -o clients/myapp
```

```python
from acme import Acme, AsyncAcme

client = Acme(api_key=..., timeout=20.0, max_retries=3)

invoice = client.invoices.create(customer_id="cus_1", amount=4200)

for inv in client.invoices.list(status="open"):  # auto-paginates
    print(inv.id)

with client.completions.stream(prompt="hi") as events:  # SSE
    for event in events:
        print(event.text)

raw = client.with_raw_response.invoices.retrieve("inv_1")  # headers, status
raw.headers["x-request-id"], raw.parse()

async with AsyncAcme() as aclient:
    page = await aclient.invoices.list()
    async for inv in aclient.invoices.list():
        ...
```

Requires Python 3.14.

## What you get

- **Code you'd write by hand.** One client object, resources as attributes,
  path parameters positional, everything else keyword-only, docstrings from the
  spec, `@deprecated` on deprecated operations. Every generated client passes
  `mypy --strict`, strict pyright, ty and ruff with zero suppressions. That's
  enforced in CI, including on Stripe's and GitHub's specs.
- **One source, two clients.** Resources are generated once as async code; the
  sync variant is derived mechanically from the AST, so the two can't drift.
- **"Omit" versus "send null".** Optional parameters default to `NOT_GIVEN`, so
  `update(memo=None)` sends `{"memo": null}` and `update()` sends `{}`.
- **Lenient in, strict out.** Responses keep unknown fields and tolerate unknown
  enum values, so a newer server never breaks an older client. Request bodies are
  validated strictly before sending, and errors name the bad argument.
- **Typed errors.** `NotFoundError`, `RateLimitError`, `UnprocessableEntityError`
  and others, each with `.body` plus `.parsed`, the error model declared for that
  status.
- **Runtime behaviour that's fixed by upgrading, not regenerating.** Retries,
  auth, pagination, streaming and uploads live in
  [`spec2py-runtime`](runtime/README.md), not in generated code.
- **Deterministic output.** The same spec and config produce byte-identical
  files, so diffs in review mean something.

## Commands

| Command | What it does |
| --- | --- |
| `spec2py generate [SPEC]` | Generate a client from a file, URL or `module:app`. `--check` fails if the client on disk is stale. |
| `spec2py fastapi module:app` | Same, from a FastAPI app, in one command. |
| `spec2py lint [SPEC]` | Explains what will generate poorly, ranked by impact. `--fix-overlay fixes.yaml` writes a starter Overlay for the mechanical fixes. |
| `spec2py update` | Regenerates, prints the API diff (breaking changes flagged) and suggests a semver bump. `--check` is for CI. |
| `spec2py diff OLD NEW` | Compares the client API two specs would produce. `--fail-on-breaking`. |
| `spec2py score [SPEC]` | Quality scorecard: size, documentation, detected pagination and streaming, approximations, and `--typecheck`. |
| `spec2py ir [SPEC]` | Prints the IR (or the normalized spec) as JSON, for debugging. |

## Inputs

Swagger 2.0, OpenAPI 3.0, 3.1 and 3.2 are supported. 2.0 and 3.0 are
up-converted to 3.1 semantics. External `$ref`s (files and URLs) are bundled.
OpenAPI 3.2 `itemSchema` types streaming events.

## Fixing a spec without forking it

Real-world specs are often wrong. Instead of editing generated code, fix the
input:

- **Overlays.** [OpenAPI Overlay](https://spec.openapis.org/overlay/latest.html)
  documents (JSONPath-targeted patches) are applied on load, so you can keep a
  small file of fixes and keep pulling the vendor's spec.
- **Config** in `pyproject.toml`:

```toml
[tool.spec2py]
spec = "https://api.acme.com/openapi.json"
output = "clients/acme"
package = "acme"
client-name = "Acme"
overlays = ["overlays/fixes.yaml"]
group-by = "tag"                    # or "path"
exclude-paths = ["/internal/*"]
idempotency-header = "Idempotency-Key"
package-version = "1.4.0"

[tool.spec2py.rename-models]
InvoiceDTO = "Invoice"

[tool.spec2py.operations."GET /invoices"]
name = "list"
resource = "invoices"
pagination = { style = "cursor", items = "data", cursor-param = "after", next-cursor = "last_id" }

[tool.spec2py.operations.createCompletion]
stream = "sse"
```

## Extending a client

Never edit generated files; regeneration overwrites them (and `--check` notices).
Instead:

- **`_custom/`.** Files in the package's `_custom/` directory are never touched.
  Names in `_custom/__init__.py`'s `__all__` are re-exported from the package root.
- **Subclasses.** `custom-resources = { invoices = "invoices:Invoices" }` makes
  the client instantiate your `_custom/invoices.py` subclass (`Invoices` and
  `AsyncInvoices`) for that resource.
- **Hooks and auth.** `Hooks(on_request=[...], on_response=[...])` for logging,
  signing and test doubles; any `httpx2.Auth` as `auth=`; `transport=` or
  `http_client=` for your own stack.

## How it works

The generator is a compiler with five stages. Each later stage reads only the
previous stage's output:

1. **Load**: a file, URL or FastAPI app; Overlays and config are applied.
2. **Normalize**: Swagger 2.0 and OpenAPI 3.0 are rewritten into 3.1 semantics,
   and external `$ref`s are bundled.
3. **Model IR**: a typed, language-neutral graph of schemas, operations and
   security schemes (`spec2py ir` prints it).
4. **Plan**: names, the schema-to-Python type mapping, resource grouping,
   signatures, and pagination and streaming detection.
5. **Emit**: Python AST (never string templates), formatted by ruff. `NOTES.md`
   records every approximation.

### Type mapping

| JSON Schema | Python |
| --- | --- |
| `object` with `properties` | `BaseModel` (`extra="allow"`) |
| required / optional field | `T` / `T \| None = None` (responses), `T \| NotGiven` (requests) |
| `nullable`, `type: [T, "null"]` | `T \| None`, absent vs null via `model_fields_set` |
| `oneOf` + `discriminator` | `Annotated[A \| B, Field(discriminator=...)]` |
| `oneOf` / `anyOf` | `A \| B` (lint warns) |
| `allOf` | inheritance from one base; extra bases are flattened, and so is a base whose field type would change |
| `enum` | `type X = Annotated[Literal[...], Tolerant]` (unknown values accepted on responses, rejected on requests) |
| `additionalProperties: {schema}` | `dict[str, T]` |
| recursive `$ref` | forward references (PEP 649) |
| `date-time`, `date`, `uuid`, `binary` | `datetime`, `date`, `UUID`, `FileTypes` in / `bytes` out |
| untyped `{}` | `object` (lint points at an Overlay fix) |

### Pagination detection

Cursor (`cursor`/`after`/`page_token`... with a next-cursor field, or Stripe-style
`starting_after` with `has_more`), offset (`offset` + `limit`), page number
(`page`), and RFC 8288 `Link` headers are all detected. You can also declare
pagination with `x-pagination` or config. Sync list methods return a `Page[T]`;
async ones return a paginator you can `await` for the first page or `async for`
over for every item.

## Development

```bash
uv sync --all-packages      # generator + runtime (uv workspace) + dev tools
uv run pytest -q            # ~230 tests, including strict type checks of generated clients
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pyright
uv run corpus/run.py --gates   # real-world specs (Stripe, GitHub, OpenAI, Twilio...) + scorecard
SPEC2PY_UPDATE_GOLDEN=1 uv run pytest tests/test_emit.py tests/test_normalize_ir.py  # accept output changes
```

Releases: push a `v*` tag. CI builds `spec2py` and `spec2py-runtime` and
publishes both to PyPI through trusted publishing.

## License

MIT

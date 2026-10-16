"""Behavioural tests: the generated Acme client against a mock Acme server."""

import json
import warnings
from types import ModuleType
from typing import Any

import httpx2
import pytest

INVOICE = {
    "id": "inv_1",
    "status": "open",
    "total": {"amount": 4200, "currency": "usd"},
    "created_at": "2026-01-01T00:00:00Z",
}


class AcmeServer:
    """Just enough of the Acme API to exercise every generated feature."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.fail_next: list[int] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        if self.fail_next:
            return httpx2.Response(self.fail_next.pop(0), json={"message": "try again"})
        path, method = request.url.path.removeprefix("/v1"), request.method
        route = getattr(self, f"{method.lower()}_{path.strip('/').replace('/', '_').replace('-', '_')}", None)
        if route is None:
            for prefix, handler in self.dynamic():
                if path.startswith(prefix) and method == handler[0]:
                    return handler[1](request, path)
            return httpx2.Response(404, json={"message": f"no route {method} {path}"})
        return route(request)

    def dynamic(self) -> list[tuple[str, tuple[str, Any]]]:
        return [
            ("/invoices/", ("GET", self.get_invoice)),
            ("/invoices/", ("PATCH", self.patch_invoice)),
            ("/invoices/", ("DELETE", lambda r, p: httpx2.Response(204))),
            ("/invoices/", ("POST", lambda r, p: httpx2.Response(200, json={**INVOICE, "status": "void"}))),
            ("/customers/", ("GET", self.payment_methods)),
            ("/files/", ("GET", lambda r, p: httpx2.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/octet-stream"}))),
        ]

    def get_invoices(self, request: httpx2.Request) -> httpx2.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx2.Response(200, json={"data": [INVOICE], "next_cursor": "c2"})
        later = {**INVOICE, "id": "inv_2", "status": "refunded", "brand_new_field": True}
        return httpx2.Response(200, json={"data": [later], "next_cursor": None})

    def post_invoices(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body["amount"] < 0:
            return httpx2.Response(422, json={"message": "invalid", "errors": [{"field": "amount", "problem": "negative"}]})
        return httpx2.Response(201, json={**INVOICE, "id": "inv_new", "total": {"amount": body["amount"], "currency": "usd"}})

    def get_invoice(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if path.endswith("missing"):
            return httpx2.Response(404, json={"message": "no such invoice", "code": "not_found"})
        return httpx2.Response(200, json=INVOICE, headers={"x-request-id": "req_123"})

    def patch_invoice(self, request: httpx2.Request, path: str) -> httpx2.Response:
        return httpx2.Response(200, json={**INVOICE, "memo": json.loads(request.content).get("memo")})

    def get_customers(self, request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params.get("offset", 0))
        people = [{"id": f"00000000-0000-0000-0000-00000000000{i}", "email": f"{i}@x.test", "class": "gold"} for i in range(3)]
        return httpx2.Response(200, json={"items": people[offset : offset + 2], "total": 3})

    def payment_methods(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if request.url.params.get("page") == "2":
            return httpx2.Response(200, json=[{"id": "pm2", "type": "bank_account", "bank_name": "B"}])
        link = f'<https://api.acme.test/v1{path}?page=2>; rel="next"'
        return httpx2.Response(200, json=[{"id": "pm1", "type": "card", "last4": "4242"}], headers={"Link": link})

    def get_catalog_categories(self, request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params.get("page", 1))
        tree = {"id": page, "name": f"c{page}", "children": [{"id": 10, "name": "leaf", "children": []}]}
        return httpx2.Response(200, json={"results": [tree], "total_pages": 2})

    def post_catalog_search(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[{"sku": "s1", "price": {"amount": 1, "currency": "usd"}}, {"id": 1, "name": "cat"}])

    def post_completions(self, request: httpx2.Request) -> httpx2.Response:
        body = b'data: {"type": "delta", "text": "Hel"}\n\ndata: {"type": "delta", "text": "lo"}\n\ndata: {"type": "done"}\n\ndata: [DONE]\n\n'
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def get_events_export(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"id": "e1", "kind": "a"}\n{"id": "e2", "kind": "b", "payload": {"x": 1}}\n')

    def post_files(self, request: httpx2.Request) -> httpx2.Response:
        assert b'filename="report.pdf"' in request.content
        return httpx2.Response(201, json={"id": "file_1", "bytes": 4, "purpose": "logo"})


@pytest.fixture
def server() -> AcmeServer:
    return AcmeServer()


@pytest.fixture
def client(acme: ModuleType, server: AcmeServer, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ACME_API_KEY", "sk_test")
    c = acme.Acme(transport=httpx2.MockTransport(server), max_retries=2)
    c._sleep = lambda seconds, reason: None
    return c


def test_auth_from_environment_and_base_url(client: Any, server: AcmeServer) -> None:
    client.invoices.retrieve("inv_1")
    req = server.requests[0]
    assert req.headers["authorization"] == "Bearer sk_test"
    assert str(req.url) == "https://api.acme.test/v1/invoices/inv_1"
    assert req.headers["user-agent"].startswith("acme/0.1.0 apismith-runtime/")


def test_explicit_credentials_and_base_url_override(acme: ModuleType, server: AcmeServer) -> None:
    c = acme.Acme(api_key="explicit", base_url="https://staging.acme.test/v1", transport=httpx2.MockTransport(server))
    c.invoices.retrieve("inv_1")
    assert server.requests[0].headers["authorization"] == "Bearer explicit"
    assert server.requests[0].url.host == "staging.acme.test"


def test_cursor_pagination_and_lenient_parsing(client: Any, server: AcmeServer) -> None:
    page = client.invoices.list(status="open", customer_ids=["c1", "c2"], limit=1)
    assert [i.id for i in page.data] == ["inv_1"]
    assert page.next_cursor == "c2"
    invoices = list(page)
    assert [i.id for i in invoices] == ["inv_1", "inv_2"]
    assert invoices[1].status == "refunded"  # unknown enum value tolerated
    assert invoices[1].model_extra == {"brand_new_field": True}
    first = server.requests[0]
    assert first.url.params.multi_items()[:4] == [("status", "open"), ("customer_ids", "c1"), ("customer_ids", "c2"), ("limit", "1")]


def test_create_validates_strictly_and_sends_wire_names(acme: ModuleType, client: Any, server: AcmeServer) -> None:
    line = acme.models.InvoiceCreateLineItem(description="Widget", amount=100)
    invoice = client.invoices.create(customer_id="cus_1", amount=4200, line_items=[line], idempotency_key="k1")
    assert invoice.total.amount == 4200
    sent = json.loads(server.requests[0].content)
    assert sent == {"customer_id": "cus_1", "amount": 4200, "line_items": [{"description": "Widget", "amount": 100}]}
    assert server.requests[0].headers["idempotency-key"] == "k1"
    with pytest.raises(Exception, match="amount"):
        client.invoices.create(customer_id="cus_1", amount="4200")
    # Bodies are validated at runtime; scalar parameters are left to type checkers.
    assert len(server.requests) == 1


def test_omit_versus_null(client: Any, server: AcmeServer) -> None:
    client.invoices.update("inv_1", memo=None)
    client.invoices.update("inv_1")
    assert json.loads(server.requests[0].content) == {"memo": None}
    assert json.loads(server.requests[1].content) == {}


def test_typed_errors_with_parsed_bodies(acme: ModuleType, client: Any) -> None:
    with pytest.raises(acme.NotFoundError) as nf:
        client.invoices.retrieve("missing")
    assert nf.value.parsed == acme.models.Error(message="no such invoice", code="not_found")
    with pytest.raises(acme.UnprocessableEntityError) as invalid:
        client.invoices.create(customer_id="c", amount=-1)
    assert isinstance(invalid.value.parsed, acme.models.ValidationError)
    assert invalid.value.parsed.errors[0].problem == "negative"


def test_retries_then_succeeds(client: Any, server: AcmeServer) -> None:
    server.fail_next = [503, 429]
    assert client.invoices.retrieve("inv_1").id == "inv_1"
    assert len(server.requests) == 3


def test_post_is_retried_safely_with_an_idempotency_key(client: Any, server: AcmeServer) -> None:
    server.fail_next = [502]
    client.invoices.create(customer_id="c", amount=1)
    keys = {r.headers["idempotency-key"] for r in server.requests}
    assert len(server.requests) == 2 and len(keys) == 1


def test_raw_responses(client: Any) -> None:
    raw = client.with_raw_response.invoices.retrieve("inv_1")
    assert raw.status_code == 200
    assert raw.headers["x-request-id"] == "req_123"
    assert raw.parse().id == "inv_1"
    assert not hasattr(client.with_raw_response.invoices, "list")  # pages carry .response


def test_offset_page_number_and_link_pagination(client: Any) -> None:
    customers = list(client.customers.list(limit=2))
    assert [c.email for c in customers] == ["0@x.test", "1@x.test", "2@x.test"]
    assert customers[0].class_ == "gold"
    categories = list(client.catalog.list_categories())
    assert [c.name for c in categories] == ["c1", "c2"]
    assert categories[0].children[0].name == "leaf"
    methods = list(client.customers.list_payment_methods("00000000-0000-0000-0000-000000000001"))
    assert [type(m).__name__ for m in methods] == ["Card", "BankAccount"]


def test_union_response_and_array_body(acme: ModuleType, client: Any, server: AcmeServer) -> None:
    results = client.catalog.search(body=[acme.models.SearchFilter(field="price", value=10, op="lt")])
    assert [type(r).__name__ for r in results] == ["Product", "Category"]
    assert json.loads(server.requests[0].content) == [{"field": "price", "value": 10, "op": "lt"}]
    assert "authorization" not in server.requests[0].headers  # security: []


def test_sse_and_ndjson_streams(client: Any) -> None:
    with client.completions.stream(prompt="hi", max_tokens=5) as events:
        assert [(e.type, e.text) for e in events] == [("delta", "Hel"), ("delta", "lo"), ("done", None)]
    with client.events.export() as events:
        assert [e.id for e in events] == ["e1", "e2"]


def test_uploads_and_downloads(client: Any, tmp_path: Any) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    uploaded = client.files.upload(file=report, purpose="logo", tags=["a"])
    assert uploaded.id == "file_1"
    assert client.files.download("file_1") == b"%PDF-1.7"


def test_deprecated_operations_warn(client: Any) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.invoices.delete("inv_1")
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_with_options_and_resources_rebind(client: Any, server: AcmeServer) -> None:
    server.fail_next = [503]
    strict = client.with_options(max_retries=0)
    with pytest.raises(Exception, match="503"):
        strict.invoices.retrieve("inv_1")
    assert strict.invoices._client is strict
    assert client.invoices._client is client


def test_docstrings_and_signatures(acme: ModuleType) -> None:
    import inspect

    assert acme.Acme.invoices.func.__name__ == "invoices"  # a cached_property
    sig = inspect.signature(acme.resources.Invoices.retrieve)
    assert list(sig.parameters)[:2] == ["self", "invoice_id"]
    assert sig.parameters["invoice_id"].kind is inspect.Parameter.POSITIONAL_ONLY
    assert sig.parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "Returns invoices, newest first." in (acme.resources.Invoices.list.__doc__ or "")


async def test_async_client_end_to_end(acme: ModuleType, server: AcmeServer) -> None:
    async with acme.AsyncAcme(api_key="k", transport=httpx2.MockTransport(server)) as client:
        page = await client.invoices.list()
        assert [i.id for i in page.data] == ["inv_1"]
        assert [i.id async for i in client.invoices.list()] == ["inv_1", "inv_2"]
        created = await client.invoices.create(customer_id="c", amount=7)
        assert created.total.amount == 7
        async with client.completions.stream(prompt="x") as events:
            assert [e.text async for e in events][:2] == ["Hel", "lo"]
        raw = await client.with_raw_response.invoices.retrieve("inv_1")
        assert raw.parse().status == "open"
        with pytest.raises(acme.NotFoundError):
            await client.invoices.retrieve("missing")

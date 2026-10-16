import json

import httpx2
import pytest

from apismith_runtime import (
    NOT_GIVEN,
    APIConnectionError,
    APIResponseValidationError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    BaseModel,
    Hooks,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    RetryPolicy,
    UnprocessableEntityError,
    build_body,
    to_raw_response_wrapper,
)
from apismith_runtime._response import APIResponse

from .helpers import Item, Recorder, async_client, client, ok


class ErrorBody(BaseModel):
    message: str
    code: str | None = None


# -- requests ----------------------------------------------------------------------


def test_builds_url_query_headers_and_json_body() -> None:
    rec = Recorder([ok({"id": "1"})])
    c = client(rec, default_headers={"X-Team": "core"}, default_query={"v": "2"})
    item = c.request(
        "post",
        "/items/a b",
        cast_to=Item,
        query={"tags": ["x", "y"], "skip": NOT_GIVEN, "flag": True},
        headers={"Idempotency-Key": "k1", "X-Absent": NOT_GIVEN},
        body={"name": "n", "missing": NOT_GIVEN},
        extra_query={"debug": 1},
        extra_headers={"X-Extra": "e"},
        extra_body={"more": True},
    )
    assert item == Item(id="1")
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.raw_path.split(b"?")[0] == b"/v1/items/a%20b"
    assert req.url.params.multi_items() == [
        ("v", "2"),
        ("tags", "x"),
        ("tags", "y"),
        ("flag", "true"),
        ("debug", "1"),
    ]
    assert req.headers["x-team"] == "core"
    assert req.headers["idempotency-key"] == "k1"
    assert req.headers["x-extra"] == "e"
    assert "x-absent" not in req.headers
    assert req.headers["user-agent"].startswith("apismith-client/")
    assert json.loads(req.content) == {"name": "n", "more": True}


def test_extra_headers_not_given_removes_a_default_header() -> None:
    rec = Recorder([ok({"id": "1"})])
    c = client(rec, default_headers={"X-Team": "core"})
    c.request("get", "/x", cast_to=Item, extra_headers={"X-Team": NOT_GIVEN})
    assert "x-team" not in rec.requests[0].headers


def test_build_body_validates_strictly_and_uses_wire_names() -> None:
    class Params(BaseModel):
        customer_id: str
        amount: int
        memo: str | None = None
        line_total: int | None = None

    Params.model_fields["line_total"].alias = "lineTotal"
    Params.model_rebuild(force=True)
    body = build_body(
        Params, {"customer_id": "c", "amount": 5, "memo": None, "line_total": NOT_GIVEN}
    )
    assert body == {"customer_id": "c", "amount": 5, "memo": None}
    with pytest.raises(Exception, match="amount"):
        build_body(Params, {"customer_id": "c", "amount": "5"})


def test_none_and_bytes_and_text_responses() -> None:
    rec = Recorder(
        [
            httpx2.Response(204),
            httpx2.Response(200, content=b"\x00\x01"),
            httpx2.Response(200, text="hi"),
        ]
    )
    c = client(rec)
    assert c.request("delete", "/x", cast_to=type(None)) is None
    assert c.request("get", "/x", cast_to=bytes) == b"\x00\x01"
    assert c.request("get", "/x", cast_to=str) == "hi"


def test_lenient_parsing_keeps_unknown_fields_and_bad_values() -> None:
    rec = Recorder([ok({"id": "1", "value": "not-a-number", "new": [1]})])
    item = client(rec).request("get", "/x", cast_to=Item)
    assert item.id == "1"
    assert item.value == "not-a-number"  # type: ignore[comparison-overlap]
    assert item.model_extra == {"new": [1]}


def test_strict_response_validation_raises() -> None:
    rec = Recorder([ok({"id": 1, "value": "nope"})])
    with pytest.raises(APIResponseValidationError):
        client(rec, strict_response_validation=True).request("get", "/x", cast_to=Item)


# -- errors ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (404, NotFoundError),
        (422, UnprocessableEntityError),
        (429, RateLimitError),
        (503, InternalServerError),
    ],
)
def test_status_errors_are_typed(status: int, exc: type[Exception]) -> None:
    rec = Recorder([ok({"message": "nope", "code": "c"}, status=status)])
    c = client(rec, max_retries=0)
    with pytest.raises(exc) as info:
        c.request("get", "/x", cast_to=Item, errors={status: ErrorBody})
    err = info.value
    assert err.status_code == status  # type: ignore[attr-defined]
    assert err.parsed == ErrorBody(message="nope", code="c")  # type: ignore[attr-defined]
    assert "nope" in str(err)


def test_error_model_fallbacks_and_unparseable_bodies() -> None:
    rec = Recorder(
        [ok({"detail": "gone"}, status=404), httpx2.Response(404, text="plain")]
    )
    c = client(rec)
    with pytest.raises(NotFoundError) as info:
        c.request("get", "/x", cast_to=Item, errors={"4XX": ErrorBody})
    assert info.value.parsed is None  # body doesn't match ErrorBody
    assert info.value.message == "gone"
    with pytest.raises(NotFoundError) as info:
        c.request("get", "/x", cast_to=Item, errors={"default": ErrorBody})
    assert info.value.body == "plain"


def test_request_id_from_headers() -> None:
    rec = Recorder([httpx2.Response(400, json={}, headers={"x-request-id": "req_1"})])
    with pytest.raises(BadRequestError) as info:
        client(rec).request("get", "/x", cast_to=Item)
    assert info.value.request_id == "req_1"


# -- retries -----------------------------------------------------------------------


def test_retries_idempotent_requests_on_retryable_statuses() -> None:
    rec = Recorder([httpx2.Response(503), httpx2.Response(429), ok({"id": "1"})])
    assert client(rec, max_retries=2).request("get", "/x", cast_to=Item).id == "1"
    assert len(rec.requests) == 3
    assert rec.requests[1].headers["x-retry-count"] == "1"
    assert rec.requests[2].headers["x-retry-count"] == "2"


def test_gives_up_after_max_retries() -> None:
    rec = Recorder([httpx2.Response(500)])
    with pytest.raises(InternalServerError):
        client(rec, max_retries=2).request("get", "/x", cast_to=Item)
    assert len(rec.requests) == 3


def test_does_not_retry_post_without_idempotency_key() -> None:
    rec = Recorder([httpx2.Response(503), ok({"id": "1"})])
    with pytest.raises(InternalServerError):
        client(rec).request("post", "/x", cast_to=Item, body={})
    assert len(rec.requests) == 1


def test_retries_post_with_generated_idempotency_key_reused_across_attempts() -> None:
    rec = Recorder([httpx2.Response(502), ok({"id": "1"})])
    c = client(rec, idempotency_header="Idempotency-Key")
    c.request("post", "/x", cast_to=Item, body={})
    keys = [r.headers["idempotency-key"] for r in rec.requests]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert keys[0].startswith("apismith-retry-")


def test_caller_idempotency_key_wins() -> None:
    rec = Recorder([ok({"id": "1"})])
    c = client(rec, idempotency_header="Idempotency-Key")
    c.request("post", "/x", cast_to=Item, headers={"Idempotency-Key": "mine"})
    assert rec.requests[0].headers["idempotency-key"] == "mine"


def test_x_should_retry_header_overrides_status() -> None:
    rec = Recorder(
        [httpx2.Response(400, headers={"x-should-retry": "true"}), ok({"id": "1"})]
    )
    assert client(rec).request("get", "/x", cast_to=Item).id == "1"
    rec = Recorder([httpx2.Response(503, headers={"x-should-retry": "false"})])
    with pytest.raises(InternalServerError):
        client(rec).request("get", "/x", cast_to=Item)
    assert len(rec.requests) == 1


def test_connection_errors_and_timeouts_retry_then_raise() -> None:
    rec = Recorder([httpx2.ConnectError("boom"), ok({"id": "1"})])
    assert client(rec).request("get", "/x", cast_to=Item).id == "1"
    rec = Recorder([httpx2.ReadTimeout("slow")])
    with pytest.raises(APITimeoutError):
        client(rec, max_retries=1).request("get", "/x", cast_to=Item)
    assert len(rec.requests) == 2
    rec = Recorder([httpx2.ConnectError("boom")])
    with pytest.raises(APIConnectionError):
        client(rec, max_retries=0).request("get", "/x", cast_to=Item)


def test_retry_delay_honours_retry_after_and_backs_off() -> None:
    policy = RetryPolicy(initial_delay=0.5, max_delay=8, jitter=0)
    assert policy.delay(0) == 0.5
    assert policy.delay(3) == 4.0
    assert policy.delay(10) == 8.0
    assert policy.delay(0, httpx2.Response(429, headers={"retry-after": "3"})) == 3.0
    assert (
        policy.delay(0, httpx2.Response(429, headers={"retry-after-ms": "250"})) == 0.25
    )
    date = "Wed, 21 Oct 2015 07:28:00 GMT"  # in the past -> retry now
    assert policy.delay(0, httpx2.Response(429, headers={"retry-after": date})) == 0.0
    # absurd hints fall back to backoff
    assert policy.delay(1, httpx2.Response(429, headers={"retry-after": "9999"})) == 1.0
    jittered = RetryPolicy(initial_delay=1, jitter=0.25)
    assert all(0.75 <= jittered.delay(0) <= 1.0 for _ in range(50))


def test_sync_client_sleeps_between_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    c = client(
        Recorder([httpx2.Response(503, headers={"retry-after": "2"}), ok({"id": "1"})])
    )
    del c._sleep  # use the real implementation (with the patched time.sleep)
    c.request("get", "/x", cast_to=Item)
    assert slept == [2.0]


# -- hooks, raw responses, options ------------------------------------------------


def test_hooks_see_every_attempt() -> None:
    seen: list[str] = []
    hooks = Hooks(
        on_request=[lambda r: seen.append(f"req {r.method}")],
        on_response=[lambda r: seen.append(f"resp {r.status_code}")],
    )
    rec = Recorder([httpx2.Response(503), ok({"id": "1"})])
    client(rec, hooks=hooks).request("get", "/x", cast_to=Item)
    assert seen == ["req GET", "resp 503", "req GET", "resp 200"]


def test_hooks_can_sign_requests() -> None:
    def sign(request: httpx2.Request) -> None:
        request.headers["X-Signature"] = f"sig:{request.url.path}"

    rec = Recorder([ok({"id": "1"})])
    client(rec, hooks=Hooks(on_request=[sign])).request("get", "/x", cast_to=Item)
    assert rec.requests[0].headers["x-signature"] == "sig:/v1/x"


def test_raw_response_wrapper() -> None:
    rec = Recorder([ok({"id": "1"}, **{"x-request-id": "abc"})])
    c = client(rec)

    def retrieve(item_id: str) -> Item:
        return c.request("get", f"/items/{item_id}", cast_to=Item)

    raw = to_raw_response_wrapper(retrieve)("1")
    assert isinstance(raw, APIResponse)
    assert raw.status_code == 200
    assert raw.headers["x-request-id"] == "abc"
    assert raw.json() == {"id": "1"}
    assert raw.parse() == Item(id="1")
    assert raw.parse() is raw.parse()
    # the context var is reset afterwards
    assert isinstance(retrieve("1"), Item)


def test_with_options_copies_without_mutating_the_original() -> None:
    rec = Recorder([httpx2.Response(503), ok({"id": "1"})])
    c = client(rec, max_retries=2, default_headers={"A": "1"})
    quick = c.with_options(max_retries=0, default_headers={"B": "2"}, timeout=1.5)
    assert c.max_retries == 2
    assert quick.max_retries == 0
    assert quick.default_headers == {"A": "1", "B": "2"}
    with pytest.raises(InternalServerError):
        quick.request("get", "/x", cast_to=Item)
    assert rec.requests[0].extensions["timeout"]["read"] == 1.5


def test_per_call_timeout() -> None:
    rec = Recorder([ok({"id": "1"})])
    client(rec).request("get", "/x", cast_to=Item, timeout=3)
    assert rec.requests[0].extensions["timeout"] == {
        "connect": 3,
        "read": 3,
        "write": 3,
        "pool": 3,
    }


def test_security_false_skips_auth() -> None:
    from apismith_runtime import BearerAuth

    rec = Recorder([ok({"id": "1"})])
    c = client(rec, auth=BearerAuth("tok"))
    c.request("get", "/x", cast_to=Item)
    c.request("get", "/public", cast_to=Item, security=False)
    assert rec.requests[0].headers["authorization"] == "Bearer tok"
    assert "authorization" not in rec.requests[1].headers


def test_context_manager_closes_owned_client() -> None:
    rec = Recorder([ok({"id": "1"})])
    with client(rec) as c:
        c.request("get", "/x", cast_to=Item)
    assert c._http.is_closed


def test_user_supplied_http_client_is_not_closed() -> None:
    from apismith_runtime import APIClient

    http = httpx2.Client(transport=httpx2.MockTransport(Recorder([ok({"id": "1"})])))
    with APIClient(base_url="https://api.test", http_client=http) as c:
        c.request("get", "/x", cast_to=Item)
    assert not http.is_closed
    http.close()


# -- async -------------------------------------------------------------------------


async def test_async_request_retries_and_hooks() -> None:
    seen: list[int] = []

    async def on_response(response: httpx2.Response) -> None:
        seen.append(response.status_code)

    rec = Recorder([httpx2.Response(500), ok({"id": "7"})])
    async with async_client(rec, hooks=Hooks(on_response=[on_response])) as c:
        item = await c.request("get", "/x", cast_to=Item)
    assert item.id == "7"
    assert seen == [500, 200]


async def test_async_errors_and_timeouts() -> None:
    rec = Recorder([ok({"message": "m"}, status=404)])
    async with async_client(rec) as c:
        with pytest.raises(NotFoundError):
            await c.request("get", "/x", cast_to=Item)
    rec = Recorder([httpx2.ReadTimeout("slow")])
    async with async_client(rec, max_retries=0) as c:
        with pytest.raises(APITimeoutError):
            await c.request("get", "/x", cast_to=Item)


async def test_async_real_sleep_uses_anyio(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("anyio.sleep", fake_sleep)
    c = async_client(
        Recorder([httpx2.Response(429, headers={"retry-after": "1"}), ok({"id": "1"})])
    )
    del c._sleep
    await c.request("get", "/x", cast_to=Item)
    assert slept == [1.0]


def test_opentelemetry_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib
    import sys
    import types

    spans: list[dict[str, object]] = []

    class Span:
        def __init__(self, name: str) -> None:
            self.attrs: dict[str, object] = {"name": name}
            spans.append(self.attrs)

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

    class Tracer:
        @contextlib.contextmanager
        def start_as_current_span(self, name: str, kind: object = None):  # type: ignore[no-untyped-def]
            yield Span(name)

    trace = types.SimpleNamespace(
        get_tracer=lambda name: Tracer(),
        SpanKind=types.SimpleNamespace(CLIENT="client"),
    )
    otel = types.ModuleType("opentelemetry")
    otel.trace = trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace)  # type: ignore[arg-type]
    rec = Recorder([httpx2.Response(503), ok({"id": "1"})])
    client(rec).request("get", "/items/{id}", cast_to=Item)
    assert spans == [
        {
            "name": "GET /items/{id}",
            "http.request.method": "GET",
            "url.template": "/items/{id}",
            "server.address": "api.test",
            "http.response.status_code": 200,
            "http.request.resend_count": 1,
        }
    ]

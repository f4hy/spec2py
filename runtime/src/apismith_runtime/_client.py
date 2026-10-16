"""The HTTP core shared by every generated client.

Generated clients subclass :class:`APIClient` / :class:`AsyncAPIClient` and their
resources subclass :class:`APIResource` / :class:`AsyncAPIResource`. All request
behaviour (retries, auth, hooks, tracing, parsing, errors) lives here, so fixing
it is a dependency upgrade rather than a regeneration.
"""

import copy
import functools
import inspect
import json
import logging
import platform
import time
import uuid
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import NoneType, TracebackType
from typing import Any, Final, Self, TypedDict, Unpack, cast

import anyio
import httpx2
from pydantic import ValidationError

from ._exceptions import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    status_error_class,
)
from ._files import to_httpx_files, to_multipart_data
from ._models import jsonable, parse_value
from ._pagination import (
    AsyncPage,
    AsyncPaginator,
    NextPage,
    Page,
    PageState,
    PaginationStrategy,
    dig,
)
from ._qs import QueryStyle, encode_query
from ._response import APIResponse, raw_response_requested
from ._retry import RetryPolicy
from ._streaming import (
    AsyncStream,
    AsyncStreamManager,
    Stream,
    StreamFormat,
    StreamManager,
)
from ._types import NOT_GIVEN, FileTypes, NotGiven, TimeoutTypes

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "APIClient",
    "APIResource",
    "AsyncAPIClient",
    "AsyncAPIResource",
    "Hooks",
    "RequestOptions",
    "RequestSpec",
]

logger = logging.getLogger("apismith")

DEFAULT_TIMEOUT: Final = httpx2.Timeout(60.0, connect=5.0)
DEFAULT_MAX_RETRIES: Final = 2

type ErrorMap = Mapping[int | str, object]
type RequestHook = Callable[[httpx2.Request], object]
type ResponseHook = Callable[[httpx2.Response], object]


@dataclass(slots=True)
class Hooks:
    """Request and response middleware.

    Hooks run on every attempt, including retries. Async clients await hooks that
    return awaitables; sync clients call them plainly. Use them for logging,
    metrics, request signing or test doubles.
    """

    on_request: list[RequestHook] = field(default_factory=list[RequestHook])
    on_response: list[ResponseHook] = field(default_factory=list[ResponseHook])


@dataclass(slots=True, kw_only=True)
class RequestSpec:
    """Everything a generated method knows about one call."""

    method: str
    path: str
    query: Mapping[str, object] | None = None
    query_styles: Mapping[str, tuple[QueryStyle, bool]] | None = None
    headers: Mapping[str, object] | None = None
    cookies: Mapping[str, object] | None = None
    body: object = NOT_GIVEN
    form: Mapping[str, object] | None = None
    files: Mapping[str, FileTypes | Sequence[FileTypes] | NotGiven] | None = None
    content: bytes | NotGiven | None = None
    content_type: str | None = None
    security: bool = True
    errors: ErrorMap | None = None
    extra_headers: Mapping[str, object] | None = None
    extra_query: Mapping[str, object] | None = None
    extra_body: Mapping[str, object] | None = None
    timeout: TimeoutTypes | NotGiven = NOT_GIVEN
    url: str | None = None


class RequestOptions(TypedDict, total=False):
    """Keyword arguments generated methods pass through to :class:`RequestSpec`."""

    query: Mapping[str, object] | None
    query_styles: Mapping[str, tuple[QueryStyle, bool]] | None
    headers: Mapping[str, object] | None
    cookies: Mapping[str, object] | None
    body: object
    form: Mapping[str, object] | None
    files: Mapping[str, FileTypes | Sequence[FileTypes] | NotGiven] | None
    content: bytes | NotGiven | None
    content_type: str | None
    security: bool
    errors: ErrorMap | None
    extra_headers: Mapping[str, object] | None
    extra_query: Mapping[str, object] | None
    extra_body: Mapping[str, object] | None
    timeout: TimeoutTypes | NotGiven


def _header_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _decode_body(response: httpx2.Response) -> object:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type or not content_type:
        try:
            return json.loads(response.content)
        except ValueError:
            return response.text
    return response.text


def _otel_tracer() -> Any:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("apismith_runtime")


class _BaseClient:
    """Request building, parsing and error mapping shared by both clients."""

    _package: str = "apismith-client"
    _version: str = "0"

    def __init__(
        self,
        *,
        base_url: str,
        auth: httpx2.Auth | None = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Mapping[str, str] | None = None,
        default_query: Mapping[str, object] | None = None,
        hooks: Hooks | None = None,
        idempotency_header: str | None = None,
        strict_response_validation: bool = False,
        instrument: bool = True,
    ) -> None:
        self.base_url = base_url
        self.auth = auth
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = dict(default_headers or {})
        self.default_query = dict(default_query or {})
        self.hooks = hooks or Hooks()
        self.idempotency_header = idempotency_header
        self.strict_response_validation = strict_response_validation
        self._tracer = _otel_tracer() if instrument else None
        self._owns_http_client = True

    @property
    def user_agent(self) -> str:
        from . import __version__

        return (
            f"{self._package}/{self._version} apismith-runtime/{__version__} "
            f"python/{platform.python_version()}"
        )

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_retries=self.max_retries)

    def with_options(
        self,
        *,
        timeout: TimeoutTypes | NotGiven = NOT_GIVEN,
        max_retries: int | NotGiven = NOT_GIVEN,
        default_headers: Mapping[str, str] | NotGiven = NOT_GIVEN,
        auth: httpx2.Auth | NotGiven | None = NOT_GIVEN,
    ) -> Self:
        """Return a copy of this client with some options changed.

        The copy shares the underlying connection pool::

            client.with_options(timeout=5, max_retries=0).invoices.retrieve("inv_1")
        """
        new = copy.copy(self)
        # Resources are cached per client; drop them so they bind to the copy.
        for klass in type(new).__mro__:
            for name, attr in vars(klass).items():
                if isinstance(attr, functools.cached_property):
                    new.__dict__.pop(name, None)
        new._owns_http_client = False
        if not isinstance(timeout, NotGiven):
            new.timeout = timeout
        if not isinstance(max_retries, NotGiven):
            new.max_retries = max_retries
        if not isinstance(default_headers, NotGiven):
            new.default_headers = {**self.default_headers, **default_headers}
        if not isinstance(auth, NotGiven):
            new.auth = auth
        return new

    # -- building ---------------------------------------------------------------

    def _build_request(
        self,
        http: httpx2.Client | httpx2.AsyncClient,
        spec: RequestSpec,
        *,
        idempotency_key: str | None,
        retries_taken: int,
    ) -> httpx2.Request:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **self.default_headers,
        }
        for source in (spec.headers, spec.extra_headers):
            for key, value in (source or {}).items():
                if isinstance(value, NotGiven):
                    headers.pop(key, None)
                elif value is not None:
                    headers[key] = _header_value(value)
        if idempotency_key and self.idempotency_header:
            headers.setdefault(self.idempotency_header, idempotency_key)
        if retries_taken:
            headers["x-retry-count"] = str(retries_taken)

        params = encode_query(self.default_query)
        params += encode_query(spec.query or {}, spec.query_styles)
        params += encode_query(spec.extra_query or {})

        if spec.cookies:
            pairs = [
                f"{k}={_header_value(v)}"
                for k, v in spec.cookies.items()
                if not isinstance(v, NotGiven) and v is not None
            ]
            if pairs:
                headers["Cookie"] = "; ".join(pairs)

        kwargs: dict[str, Any] = {}
        if spec.files or spec.form is not None:
            form = dict(spec.form or {})
            form.update(spec.extra_body or {})
            if spec.files:
                kwargs["files"] = to_httpx_files(spec.files)
                kwargs["data"] = to_multipart_data(form)
            else:
                kwargs["data"] = to_multipart_data(form)
        elif spec.content is not None and not isinstance(spec.content, NotGiven):
            kwargs["content"] = spec.content
            headers.setdefault(
                "Content-Type", spec.content_type or "application/octet-stream"
            )
        elif not isinstance(spec.body, NotGiven) or spec.extra_body:
            body: object = {}
            if not isinstance(spec.body, NotGiven):
                body = jsonable(spec.body)
            if spec.extra_body:
                if not isinstance(body, dict):
                    raise TypeError("extra_body requires a JSON object request body")
                body = {**cast(dict[str, object], body), **spec.extra_body}
            kwargs["json"] = body
            if spec.content_type:
                headers["Content-Type"] = spec.content_type

        timeout = self.timeout if isinstance(spec.timeout, NotGiven) else spec.timeout
        url = spec.url or spec.path
        return http.build_request(
            spec.method.upper(),
            url,
            params=cast(Any, params) or None,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )

    def _new_idempotency_key(self, spec: RequestSpec) -> str | None:
        if self.idempotency_header and spec.method.upper() not in {"GET", "HEAD"}:
            return f"apismith-retry-{uuid.uuid4()}"
        return None

    def _auth_for(self, spec: RequestSpec) -> httpx2.Auth | None:
        return self.auth if spec.security else None

    # -- parsing ----------------------------------------------------------------

    def _make_status_error(
        self, response: httpx2.Response, errors: ErrorMap | None
    ) -> APIStatusError:
        body = _decode_body(response)
        parsed: object | None = None
        status = response.status_code
        model = None
        if errors:
            model = (
                errors.get(status)
                or errors.get(f"{status // 100}XX")
                or errors.get("default")
            )
        if model is not None and body is not None:
            try:
                parsed = parse_value(cast(Any, model), body, strict=True)
            except ValidationError:
                parsed = None
        message = self._error_message(body) or response.reason_phrase or "Error"
        return status_error_class(status)(
            message, response=response, body=body, parsed=parsed
        )

    @staticmethod
    def _error_message(body: object) -> str | None:
        if isinstance(body, str):
            return body[:500] or None
        if isinstance(body, dict):
            data = cast(dict[str, object], body)
            for key in ("message", "detail", "error_description", "title"):
                if isinstance(data.get(key), str):
                    return cast(str, data[key])
            error = data.get("error")
            if isinstance(error, str):
                return error
            if isinstance(error, dict):
                message = cast(dict[str, object], error).get("message")
                if isinstance(message, str):
                    return message
            return json.dumps(data)[:500]
        return None

    def _parse[T](self, response: httpx2.Response, cast_to: type[T]) -> T:
        if cast_to is NoneType:
            return cast(T, None)
        if cast_to is httpx2.Response:
            return cast(T, response)
        if cast_to is bytes:
            return cast(T, response.content)
        if cast_to is str:
            return cast(T, response.text)
        body = _decode_body(response)
        try:
            return parse_value(cast_to, body, strict=self.strict_response_validation)
        except ValidationError as exc:
            raise APIResponseValidationError(response, body, str(exc)) from exc

    def _result[T](self, response: httpx2.Response, cast_to: type[T]) -> T:
        if raw_response_requested():
            raw = APIResponse(response, lambda: self._parse(response, cast_to))
            return cast(T, raw)
        return self._parse(response, cast_to)

    def _page_parts[T](
        self,
        response: httpx2.Response,
        item_type: type[T],
        strategy: PaginationStrategy,
        params: dict[str, object],
    ) -> tuple[list[T], PageState]:
        body = _decode_body(response)
        raw_items = dig(body, strategy.items)
        items = cast(list[object], raw_items) if isinstance(raw_items, list) else []
        data = parse_value(
            list[item_type],  # type: ignore[valid-type]
            items,
            strict=self.strict_response_validation,
        )
        state = PageState(
            params=params, body=body, headers=response.headers, items=items
        )
        return data, state

    @contextmanager
    def _span(self, spec: RequestSpec) -> Generator[Any]:
        if self._tracer is None:
            yield None
            return
        name = f"{spec.method.upper()} {spec.path}"
        with self._tracer.start_as_current_span(name, kind=_client_kind()) as span:
            span.set_attribute("http.request.method", spec.method.upper())
            span.set_attribute("url.template", spec.path)
            span.set_attribute("server.address", httpx2.URL(self.base_url).host)
            yield span


def _client_kind() -> Any:
    from opentelemetry.trace import SpanKind

    return SpanKind.CLIENT


def _next_spec(spec: RequestSpec, next_page: NextPage) -> RequestSpec:
    new = copy.copy(spec)
    if next_page.url is not None:
        new.url = next_page.url
        new.query = None
        new.query_styles = None
    else:
        new.query = {**(spec.query or {}), **next_page.params}
    return new


def _query_dict(spec: RequestSpec) -> dict[str, object]:
    return {k: v for k, v in (spec.query or {}).items() if not isinstance(v, NotGiven)}


class APIClient(_BaseClient):
    """Synchronous HTTP core. Generated sync clients subclass this."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: httpx2.Auth | None = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Mapping[str, str] | None = None,
        default_query: Mapping[str, object] | None = None,
        hooks: Hooks | None = None,
        idempotency_header: str | None = None,
        strict_response_validation: bool = False,
        instrument: bool = True,
        http_client: httpx2.Client | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            default_query=default_query,
            hooks=hooks,
            idempotency_header=idempotency_header,
            strict_response_validation=strict_response_validation,
            instrument=instrument,
        )
        self._owns_http_client = http_client is None
        self._http = http_client or httpx2.Client(
            base_url=base_url, timeout=timeout, transport=transport
        )
        if http_client is not None:
            self._http.base_url = httpx2.URL(base_url)

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _send(self, spec: RequestSpec, *, stream: bool = False) -> httpx2.Response:
        policy = self.retry_policy
        key = self._new_idempotency_key(spec)
        with self._span(spec) as span:
            retries_taken = 0
            while True:
                request = self._build_request(
                    self._http, spec, idempotency_key=key, retries_taken=retries_taken
                )
                retryable = retries_taken < policy.max_retries and policy.can_retry(
                    request, self.idempotency_header
                )
                for hook in self.hooks.on_request:
                    hook(request)
                try:
                    auth = self._auth_for(spec)
                    response = self._http.send(request, stream=stream, auth=auth)
                except httpx2.TimeoutException as exc:
                    if retryable:
                        retries_taken += 1
                        self._sleep(policy.delay(retries_taken - 1), "timeout")
                        continue
                    raise APITimeoutError(request) from exc
                except httpx2.TransportError as exc:
                    if retryable:
                        retries_taken += 1
                        self._sleep(policy.delay(retries_taken - 1), type(exc).__name__)
                        continue
                    raise APIConnectionError(request) from exc
                for response_hook in self.hooks.on_response:
                    response_hook(response)
                if span is not None:
                    span.set_attribute(
                        "http.response.status_code", response.status_code
                    )
                    span.set_attribute("http.request.resend_count", retries_taken)
                if response.is_error:
                    if retryable and policy.should_retry_response(response):
                        response.close()
                        retries_taken += 1
                        delay = policy.delay(retries_taken - 1, response)
                        self._sleep(delay, str(response.status_code))
                        continue
                    if stream:
                        response.read()
                        response.close()
                    raise self._make_status_error(response, spec.errors)
                return response

    @staticmethod
    def _sleep(seconds: float, reason: str) -> None:
        logger.info("Retrying request in %.2fs (%s)", seconds, reason)
        time.sleep(seconds)

    def request[T](
        self,
        method: str,
        path: str,
        *,
        cast_to: type[T],
        **kwargs: Unpack[RequestOptions],
    ) -> T:
        """Send one request and parse its body into ``cast_to``."""
        spec = RequestSpec(method=method, path=path, **kwargs)
        return self._result(self._send(spec), cast_to)

    def get_api_list[T](
        self,
        method: str,
        path: str,
        *,
        item_type: type[T],
        strategy: PaginationStrategy,
        **kwargs: Unpack[RequestOptions],
    ) -> Page[T]:
        """Fetch the first page of a paginated operation."""
        spec = RequestSpec(method=method, path=path, **kwargs)

        def fetch(spec: RequestSpec) -> Page[T]:
            response = self._send(spec)
            data, state = self._page_parts(
                response, item_type, strategy, _query_dict(spec)
            )
            return Page(
                data=data,
                response=response,
                state=state,
                strategy=strategy,
                fetch=lambda next_page: fetch(_next_spec(spec, next_page)),
            )

        return fetch(spec)

    def stream[T](
        self,
        method: str,
        path: str,
        *,
        event_type: type[T],
        stream_format: StreamFormat = "sse",
        terminator: str | None = "[DONE]",
        **kwargs: Unpack[RequestOptions],
    ) -> StreamManager[T]:
        """Prepare a streaming request; it is sent when the manager is entered."""
        spec = RequestSpec(method=method, path=path, **kwargs)
        accept = (
            "text/event-stream" if stream_format == "sse" else "application/x-ndjson"
        )
        spec.extra_headers = {"Accept": accept, **(spec.extra_headers or {})}

        def open_stream() -> Stream[T]:
            response = self._send(spec, stream=True)
            return Stream(
                response,
                event_type=event_type,
                stream_format=stream_format,
                terminator=terminator,
            )

        return StreamManager(open_stream)


class AsyncAPIClient(_BaseClient):
    """Asynchronous HTTP core. Generated async clients subclass this."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: httpx2.Auth | None = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Mapping[str, str] | None = None,
        default_query: Mapping[str, object] | None = None,
        hooks: Hooks | None = None,
        idempotency_header: str | None = None,
        strict_response_validation: bool = False,
        instrument: bool = True,
        http_client: httpx2.AsyncClient | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            default_query=default_query,
            hooks=hooks,
            idempotency_header=idempotency_header,
            strict_response_validation=strict_response_validation,
            instrument=instrument,
        )
        self._owns_http_client = http_client is None
        self._http = http_client or httpx2.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport
        )
        if http_client is not None:
            self._http.base_url = httpx2.URL(base_url)

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @staticmethod
    async def _call_hook(hook: Callable[[Any], object], arg: object) -> None:
        result = hook(arg)
        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)

    async def _send(
        self, spec: RequestSpec, *, stream: bool = False
    ) -> httpx2.Response:
        policy = self.retry_policy
        key = self._new_idempotency_key(spec)
        with self._span(spec) as span:
            retries_taken = 0
            while True:
                request = self._build_request(
                    self._http, spec, idempotency_key=key, retries_taken=retries_taken
                )
                retryable = retries_taken < policy.max_retries and policy.can_retry(
                    request, self.idempotency_header
                )
                for hook in self.hooks.on_request:
                    await self._call_hook(hook, request)
                try:
                    auth = self._auth_for(spec)
                    response = await self._http.send(request, stream=stream, auth=auth)
                except httpx2.TimeoutException as exc:
                    if retryable:
                        retries_taken += 1
                        await self._sleep(policy.delay(retries_taken - 1), "timeout")
                        continue
                    raise APITimeoutError(request) from exc
                except httpx2.TransportError as exc:
                    if retryable:
                        retries_taken += 1
                        await self._sleep(
                            policy.delay(retries_taken - 1), type(exc).__name__
                        )
                        continue
                    raise APIConnectionError(request) from exc
                for response_hook in self.hooks.on_response:
                    await self._call_hook(response_hook, response)
                if span is not None:
                    span.set_attribute(
                        "http.response.status_code", response.status_code
                    )
                    span.set_attribute("http.request.resend_count", retries_taken)
                if response.is_error:
                    if retryable and policy.should_retry_response(response):
                        await response.aclose()
                        retries_taken += 1
                        delay = policy.delay(retries_taken - 1, response)
                        await self._sleep(delay, str(response.status_code))
                        continue
                    if stream:
                        await response.aread()
                        await response.aclose()
                    raise self._make_status_error(response, spec.errors)
                return response

    @staticmethod
    async def _sleep(seconds: float, reason: str) -> None:
        logger.info("Retrying request in %.2fs (%s)", seconds, reason)
        await anyio.sleep(seconds)

    async def request[T](
        self,
        method: str,
        path: str,
        *,
        cast_to: type[T],
        **kwargs: Unpack[RequestOptions],
    ) -> T:
        """Send one request and parse its body into ``cast_to``."""
        spec = RequestSpec(method=method, path=path, **kwargs)
        return self._result(await self._send(spec), cast_to)

    def get_api_list[T](
        self,
        method: str,
        path: str,
        *,
        item_type: type[T],
        strategy: PaginationStrategy,
        **kwargs: Unpack[RequestOptions],
    ) -> AsyncPaginator[T]:
        """Return a paginator: ``await`` it for the first page or iterate every item."""
        spec = RequestSpec(method=method, path=path, **kwargs)

        async def fetch(spec: RequestSpec) -> AsyncPage[T]:
            response = await self._send(spec)
            data, state = self._page_parts(
                response, item_type, strategy, _query_dict(spec)
            )
            return AsyncPage(
                data=data,
                response=response,
                state=state,
                strategy=strategy,
                fetch=lambda next_page: fetch(_next_spec(spec, next_page)),
            )

        return AsyncPaginator(lambda: fetch(spec))

    def stream[T](
        self,
        method: str,
        path: str,
        *,
        event_type: type[T],
        stream_format: StreamFormat = "sse",
        terminator: str | None = "[DONE]",
        **kwargs: Unpack[RequestOptions],
    ) -> AsyncStreamManager[T]:
        """Prepare a streaming request; it is sent when the manager is entered."""
        spec = RequestSpec(method=method, path=path, **kwargs)
        accept = (
            "text/event-stream" if stream_format == "sse" else "application/x-ndjson"
        )
        spec.extra_headers = {"Accept": accept, **(spec.extra_headers or {})}

        async def open_stream() -> AsyncStream[T]:
            response = await self._send(spec, stream=True)
            return AsyncStream(
                response,
                event_type=event_type,
                stream_format=stream_format,
                terminator=terminator,
            )

        return AsyncStreamManager(open_stream)


class APIResource:
    """Base for generated sync resources."""

    def __init__(self, client: APIClient) -> None:
        self._client = client


class AsyncAPIResource:
    """Base for generated async resources."""

    def __init__(self, client: AsyncAPIClient) -> None:
        self._client = client

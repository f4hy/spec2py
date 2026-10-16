"""Raw responses: status, headers and the undecoded body, alongside the typed parse.

::

    raw = client.with_raw_response.invoices.retrieve("inv_1")
    raw.status_code, raw.headers["x-request-id"]
    invoice = raw.parse()
"""

import contextvars
import functools
import json
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

import httpx2

__all__ = [
    "APIResponse",
    "async_to_raw_response_wrapper",
    "raw_response_requested",
    "to_raw_response_wrapper",
]

_RAW_RESPONSE: Final = contextvars.ContextVar("spec2py_raw_response", default=False)


def raw_response_requested() -> bool:
    return _RAW_RESPONSE.get()


class APIResponse[T]:
    """An HTTP response plus a lazy, typed ``parse()``."""

    def __init__(self, http_response: httpx2.Response, parse: Callable[[], T]) -> None:
        self.http_response = http_response
        self._parse = parse
        self._parsed: list[T] = []

    @property
    def status_code(self) -> int:
        return self.http_response.status_code

    @property
    def headers(self) -> httpx2.Headers:
        return self.http_response.headers

    @property
    def content(self) -> bytes:
        return self.http_response.content

    @property
    def text(self) -> str:
        return self.http_response.text

    def json(self) -> Any:
        return json.loads(self.http_response.content)

    def parse(self) -> T:
        """Parse the body into the operation's declared return type (cached)."""
        if not self._parsed:
            self._parsed.append(self._parse())
        return self._parsed[0]

    def __repr__(self) -> str:
        return f"<APIResponse [{self.status_code}] {self.http_response.url}>"


def to_raw_response_wrapper[**P, R](
    func: Callable[P, R],
) -> Callable[P, APIResponse[R]]:
    """Wrap a sync resource method so it returns :class:`APIResponse` instead."""

    @functools.wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> APIResponse[R]:
        token = _RAW_RESPONSE.set(True)
        try:
            return cast(APIResponse[R], func(*args, **kwargs))
        finally:
            _RAW_RESPONSE.reset(token)

    return wrapped


def async_to_raw_response_wrapper[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[APIResponse[R]]]:
    """Wrap an async resource method so it returns :class:`APIResponse` instead."""

    @functools.wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> APIResponse[R]:
        token = _RAW_RESPONSE.set(True)
        try:
            return cast(APIResponse[R], await func(*args, **kwargs))
        finally:
            _RAW_RESPONSE.reset(token)

    return wrapped

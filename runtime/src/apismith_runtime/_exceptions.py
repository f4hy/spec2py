"""Typed error hierarchy raised by generated clients.

::

    APIError
    ├── APIConnectionError
    │   └── APITimeoutError
    ├── APIResponseValidationError
    └── APIStatusError
        ├── BadRequestError            400
        ├── AuthenticationError        401
        ├── PermissionDeniedError      403
        ├── NotFoundError              404
        ├── ConflictError              409
        ├── UnprocessableEntityError   422
        ├── RateLimitError             429
        └── InternalServerError        5xx
"""

from typing import override

import httpx2

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIResponseValidationError",
    "APIStatusError",
    "APITimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "InternalServerError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "UnprocessableEntityError",
    "status_error_class",
]


class APIError(Exception):
    """Base class for every error a generated client raises."""

    message: str
    request: httpx2.Request

    def __init__(self, message: str, request: httpx2.Request) -> None:
        super().__init__(message)
        self.message = message
        self.request = request


class APIConnectionError(APIError):
    """The request never produced a response (DNS, TCP, TLS or protocol failure)."""

    def __init__(
        self, request: httpx2.Request, message: str = "Connection error."
    ) -> None:
        super().__init__(message, request)


class APITimeoutError(APIConnectionError):
    """The request timed out, after any retries."""

    def __init__(self, request: httpx2.Request) -> None:
        super().__init__(request, "Request timed out.")


class APIResponseValidationError(APIError):
    """A 2xx response body did not match the declared schema at all."""

    response: httpx2.Response
    body: object

    def __init__(
        self, response: httpx2.Response, body: object, message: str | None = None
    ) -> None:
        super().__init__(
            message or "Response body did not match the expected schema.",
            response.request,
        )
        self.response = response
        self.body = body


class APIStatusError(APIError):
    """The server answered with a 4xx or 5xx status.

    ``body`` is the decoded JSON (or text) body. ``parsed`` is that body parsed
    into the error model the spec declares for this status, when one exists and
    the body matches it.
    """

    response: httpx2.Response
    status_code: int
    body: object
    parsed: object | None

    def __init__(
        self,
        message: str,
        *,
        response: httpx2.Response,
        body: object,
        parsed: object | None = None,
    ) -> None:
        super().__init__(message, response.request)
        self.response = response
        self.status_code = response.status_code
        self.body = body
        self.parsed = parsed

    @property
    def request_id(self) -> str | None:
        headers = self.response.headers
        return headers.get("x-request-id") or headers.get("request-id")

    @override
    def __str__(self) -> str:
        return f"{self.status_code} {self.message}"


class BadRequestError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class NotFoundError(APIStatusError):
    pass


class ConflictError(APIStatusError):
    pass


class UnprocessableEntityError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


_BY_STATUS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def status_error_class(status_code: int) -> type[APIStatusError]:
    """Pick the most specific :class:`APIStatusError` subclass for a status."""
    if status_code >= 500:
        return InternalServerError
    return _BY_STATUS.get(status_code, APIStatusError)

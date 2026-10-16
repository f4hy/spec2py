"""Authentication schemes as ``httpx2.Auth`` flows.

Every scheme here works for both the sync and async clients, because httpx2
drives the same generator-based ``auth_flow`` in both modes. Custom auth is any
``httpx2.Auth`` subclass passed as ``auth=`` to a generated client.
"""

import base64
import time
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from typing import Literal, override

import httpx2

__all__ = [
    "ApiKeyAuth",
    "BasicAuth",
    "BearerAuth",
    "MultiAuth",
    "OAuth2ClientCredentials",
]

type AuthFlow = Generator[httpx2.Request, httpx2.Response]


class ApiKeyAuth(httpx2.Auth):
    """An API key sent in a header, query parameter or cookie."""

    def __init__(
        self,
        key: str,
        *,
        name: str,
        location: Literal["header", "query", "cookie"] = "header",
        prefix: str | None = None,
    ) -> None:
        self.key = key
        self.name = name
        self.location = location
        self.prefix = prefix

    @override
    def auth_flow(self, request: httpx2.Request) -> AuthFlow:
        value = f"{self.prefix} {self.key}" if self.prefix else self.key
        match self.location:
            case "header":
                request.headers[self.name] = value
            case "query":
                request.url = request.url.copy_merge_params({self.name: value})
            case "cookie":
                existing = request.headers.get("cookie")
                pair = f"{self.name}={value}"
                request.headers["cookie"] = f"{existing}; {pair}" if existing else pair
            case _:
                raise ValueError(f"unknown API key location {self.location!r}")
        yield request


class BearerAuth(httpx2.Auth):
    """``Authorization: Bearer <token>``."""

    def __init__(self, token: str) -> None:
        self.token = token

    @override
    def auth_flow(self, request: httpx2.Request) -> AuthFlow:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class BasicAuth(httpx2.Auth):
    """HTTP basic auth."""

    def __init__(self, username: str, password: str) -> None:
        raw = f"{username}:{password}".encode()
        self._header = "Basic " + base64.b64encode(raw).decode("ascii")

    @override
    def auth_flow(self, request: httpx2.Request) -> AuthFlow:
        request.headers["Authorization"] = self._header
        yield request


class MultiAuth(httpx2.Auth):
    """Apply several schemes to every request, e.g. an API key plus a bearer token."""

    def __init__(self, auths: Sequence[httpx2.Auth]) -> None:
        self.auths = list(auths)

    @override
    def auth_flow(self, request: httpx2.Request) -> AuthFlow:
        yield from self._chain(0, request)

    def _chain(
        self, index: int, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, httpx2.Response]:
        if index == len(self.auths):
            return (yield request)
        flow = self.auths[index].auth_flow(request)
        outgoing = next(flow)
        while True:
            if outgoing is request:
                response = yield from self._chain(index + 1, outgoing)
            else:
                # A side request of this scheme (e.g. an OAuth2 token fetch):
                # send it as-is, without the other schemes' credentials.
                response = yield outgoing
            try:
                outgoing = flow.send(response)
            except StopIteration:
                return response


@dataclass(slots=True)
class _Token:
    access_token: str
    expires_at: float | None
    refresh_token: str | None = None

    def valid(self, leeway: float) -> bool:
        return self.expires_at is None or time.time() < self.expires_at - leeway


@dataclass
class OAuth2ClientCredentials(httpx2.Auth):
    """OAuth2 client-credentials grant with automatic token fetch and refresh.

    Fetches a token on first use, reuses it until shortly before it expires,
    uses the refresh token when the server issued one, and retries once with a
    fresh token if the API answers 401.
    """

    token_url: str
    client_id: str
    client_secret: str
    scopes: Sequence[str] = ()
    audience: str | None = None
    leeway: float = 30.0
    requires_response_body = True
    _token: _Token | None = field(default=None, init=False, repr=False)

    def _token_request(self) -> httpx2.Request:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self._token and self._token.refresh_token:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._token.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        if self.scopes:
            data["scope"] = " ".join(self.scopes)
        if self.audience:
            data["audience"] = self.audience
        return httpx2.Request("POST", self.token_url, data=data)

    def _store(self, response: httpx2.Response) -> _Token:
        response.raise_for_status()
        payload = response.json()
        expires_in = payload.get("expires_in")
        self._token = _Token(
            access_token=payload["access_token"],
            expires_at=time.time() + float(expires_in) if expires_in else None,
            refresh_token=payload.get("refresh_token"),
        )
        return self._token

    @override
    def auth_flow(self, request: httpx2.Request) -> AuthFlow:
        token = self._token
        if token is None or not token.valid(self.leeway):
            token = self._store((yield self._token_request()))
        request.headers["Authorization"] = f"Bearer {token.access_token}"
        response = yield request
        if response.status_code == 401:
            self._token = None
            token = self._store((yield self._token_request()))
            request.headers["Authorization"] = f"Bearer {token.access_token}"
            yield request

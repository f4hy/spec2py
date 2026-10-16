"""Fault-injecting transports and a tiny client/resource for runtime tests."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx2

from spec2py_runtime import APIClient, AsyncAPIClient, BaseModel, RetryPolicy

type Handler = Callable[[httpx2.Request], httpx2.Response]


class Item(BaseModel):
    id: str
    value: int | None = None


@dataclass
class Recorder:
    """Replays scripted responses (or exceptions) and records every request."""

    script: list[httpx2.Response | Exception | Handler]
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step

    def bodies(self) -> Iterator[object]:
        for r in self.requests:
            yield json.loads(r.content) if r.content else None


class FastRetries(RetryPolicy):
    """Retry policy without real sleeping (delays are recorded instead)."""


def client(handler: Handler, **kwargs: object) -> APIClient:
    c = APIClient(
        base_url="https://api.test/v1",
        transport=httpx2.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )
    c._sleep = lambda seconds, reason: None  # type: ignore[method-assign]
    return c


def async_client(handler: Handler, **kwargs: object) -> AsyncAPIClient:
    c = AsyncAPIClient(
        base_url="https://api.test/v1",
        transport=httpx2.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )

    async def no_sleep(seconds: float, reason: str) -> None:
        return None

    c._sleep = no_sleep  # type: ignore[method-assign]
    return c


def ok(payload: object, status: int = 200, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, json=payload, headers=headers)

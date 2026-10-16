"""Server-sent events and NDJSON streams decoded into typed events.

::

    with client.completions.stream(prompt="hi") as events:
        for event in events:
            print(event.text)
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self, cast

import httpx2

from ._exceptions import APIStatusError
from ._models import parse_value

__all__ = [
    "AsyncStream",
    "AsyncStreamManager",
    "SSEDecoder",
    "ServerSentEvent",
    "Stream",
    "StreamFormat",
    "StreamManager",
]

type StreamFormat = Literal["sse", "ndjson"]


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """One raw event from a ``text/event-stream`` response."""

    data: str
    event: str = "message"
    id: str | None = None
    retry: int | None = None

    def json(self) -> Any:
        return json.loads(self.data)


class SSEDecoder:
    """Incremental decoder for the WHATWG server-sent events line protocol."""

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []
        self._id: str | None = None
        self._last_id: str | None = None
        self._retry: int | None = None

    def decode(self, line: str) -> ServerSentEvent | None:
        """Feed one line (without its terminator); return an event on a blank line."""
        if not line:
            if not self._data and self._event is None:
                return None
            event = ServerSentEvent(
                data="\n".join(self._data),
                event=self._event or "message",
                id=self._last_id,
                retry=self._retry,
            )
            self._event, self._data, self._retry = None, [], None
            return event
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        match name:
            case "event":
                self._event = value
            case "data":
                self._data.append(value)
            case "id" if "\0" not in value:
                self._last_id = value
            case "retry" if value.isdigit():
                self._retry = int(value)
            case _:
                pass
        return None

    def flush(self) -> ServerSentEvent | None:
        return self.decode("")


class _Decoding[T]:
    def __init__(
        self,
        response: httpx2.Response,
        *,
        event_type: type[T],
        stream_format: StreamFormat,
        terminator: str | None,
    ) -> None:
        self.response = response
        self._event_type = event_type
        self._format = stream_format
        self._terminator = terminator

    def _parse(self, data: str) -> T:
        if self._event_type is str:
            return cast(T, data)
        return parse_value(self._event_type, json.loads(data))

    def _raise_error_event(self, sse: ServerSentEvent) -> None:
        try:
            body: object = sse.json()
        except ValueError:
            body = sse.data
        raise APIStatusError(
            "The server sent an error event.", response=self.response, body=body
        )

    def _handle_sse(self, sse: ServerSentEvent) -> tuple[bool, T | None]:
        """Return ``(stop, event)`` for one raw event."""
        if self._terminator is not None and sse.data == self._terminator:
            return True, None
        if sse.event == "error":
            self._raise_error_event(sse)
        if not sse.data:
            return False, None
        return False, self._parse(sse.data)


class Stream[T](_Decoding[T]):
    """A synchronous iterator of typed events over an open streaming response."""

    def __iter__(self) -> Iterator[T]:
        if self._format == "ndjson":
            for line in self.response.iter_lines():
                if line.strip():
                    yield self._parse(line)
            return
        for sse in self.events():
            stop, event = self._handle_sse(sse)
            if stop:
                return
            if event is not None:
                yield event

    def events(self) -> Iterator[ServerSentEvent]:
        """Iterate the raw server-sent events, undecoded."""
        decoder = SSEDecoder()
        for line in self.response.iter_lines():
            if (sse := decoder.decode(line)) is not None:
                yield sse
        if (sse := decoder.flush()) is not None:
            yield sse

    def close(self) -> None:
        self.response.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncStream[T](_Decoding[T]):
    """An asynchronous iterator of typed events over an open streaming response."""

    async def __aiter__(self) -> AsyncIterator[T]:
        if self._format == "ndjson":
            async for line in self.response.aiter_lines():
                if line.strip():
                    yield self._parse(line)
            return
        async for sse in self.events():
            stop, event = self._handle_sse(sse)
            if stop:
                return
            if event is not None:
                yield event

    async def events(self) -> AsyncIterator[ServerSentEvent]:
        """Iterate the raw server-sent events, undecoded."""
        decoder = SSEDecoder()
        async for line in self.response.aiter_lines():
            if (sse := decoder.decode(line)) is not None:
                yield sse
        if (sse := decoder.flush()) is not None:
            yield sse

    async def close(self) -> None:
        await self.response.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class StreamManager[T]:
    """Context manager that opens the stream on enter and closes it on exit."""

    def __init__(self, open_stream: Callable[[], Stream[T]]) -> None:
        self._open = open_stream
        self._stream: Stream[T] | None = None

    def __enter__(self) -> Stream[T]:
        self._stream = self._open()
        return self._stream

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            self._stream.close()


class AsyncStreamManager[T]:
    """Async context manager that opens the stream on enter and closes it on exit."""

    def __init__(self, open_stream: Callable[[], Awaitable[AsyncStream[T]]]) -> None:
        self._open = open_stream
        self._stream: AsyncStream[T] | None = None

    async def __aenter__(self) -> AsyncStream[T]:
        self._stream = await self._open()
        return self._stream

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            await self._stream.close()

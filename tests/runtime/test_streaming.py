import httpx2
import pytest

from apismith_runtime import APIStatusError, BaseModel, NotFoundError, SSEDecoder
from apismith_runtime._streaming import ServerSentEvent

from .helpers import Recorder, async_client, client


class Delta(BaseModel):
    text: str


def sse(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(
        status, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


def test_sse_decoder_follows_the_spec() -> None:
    decoder = SSEDecoder()
    lines = [
        ": comment",
        "event: update",
        "id: 7",
        "retry: 1500",
        "data: line one",
        "data:line two",
        "",
        "data: second",
        "",
        "",
    ]
    events = [e for line in lines if (e := decoder.decode(line)) is not None]
    assert events == [
        ServerSentEvent(data="line one\nline two", event="update", id="7", retry=1500),
        ServerSentEvent(data="second", event="message", id="7"),
    ]


def test_sync_stream_yields_typed_events_until_done() -> None:
    body = 'data: {"text": "he"}\n\ndata: {"text": "llo"}\n\ndata: [DONE]\n\ndata: {"text": "ignored"}\n\n'
    rec = Recorder([sse(body)])
    with client(rec).stream("post", "/complete", event_type=Delta, body={}) as events:
        assert [e.text for e in events] == ["he", "llo"]
    assert rec.requests[0].headers["accept"] == "text/event-stream"


def test_stream_raw_events_and_str_event_type() -> None:
    rec = Recorder([sse("event: a\ndata: x\n\nevent: b\ndata: y\n\n")])
    with client(rec).stream("get", "/s", event_type=str, terminator=None) as events:
        assert [e for e in events] == ["x", "y"]
    rec = Recorder([sse("event: a\ndata: x\n\n")])
    with client(rec).stream("get", "/s", event_type=str) as events:
        assert [(e.event, e.data) for e in events.events()] == [("a", "x")]


def test_error_event_raises() -> None:
    rec = Recorder([sse('data: {"text": "a"}\n\nevent: error\ndata: {"message": "boom"}\n\n')])
    with client(rec).stream("get", "/s", event_type=Delta) as events:
        it = iter(events)
        assert next(it).text == "a"
        with pytest.raises(APIStatusError) as info:
            next(it)
    assert info.value.body == {"message": "boom"}


def test_ndjson_stream() -> None:
    body = '{"text": "a"}\n\n{"text": "b"}\n'
    rec = Recorder([httpx2.Response(200, content=body.encode())])
    with client(rec).stream("get", "/s", event_type=Delta, stream_format="ndjson") as events:
        assert [e.text for e in events] == ["a", "b"]
    assert rec.requests[0].headers["accept"] == "application/x-ndjson"


def test_stream_error_status_raises_on_enter() -> None:
    rec = Recorder([httpx2.Response(404, json={"message": "nope"})])
    with pytest.raises(NotFoundError):
        with client(rec).stream("get", "/s", event_type=Delta):
            pass


def test_stream_retries_before_streaming() -> None:
    rec = Recorder([httpx2.Response(503), sse('data: {"text": "ok"}\n\n')])
    with client(rec).stream("get", "/s", event_type=Delta) as events:
        assert [e.text for e in events] == ["ok"]
    assert len(rec.requests) == 2


async def test_async_stream() -> None:
    rec = Recorder([sse('data: {"text": "a"}\n\ndata: {"text": "b"}\n\ndata: [DONE]\n\n')])
    async with async_client(rec) as c:
        async with c.stream("post", "/s", event_type=Delta) as events:
            assert [e.text async for e in events] == ["a", "b"]


async def test_async_ndjson_and_raw_events() -> None:
    rec = Recorder([httpx2.Response(200, content=b'{"text": "a"}\n')])
    async with async_client(rec) as c:
        async with c.stream("get", "/s", event_type=Delta, stream_format="ndjson") as ev:
            assert [e.text async for e in ev] == ["a"]
    rec = Recorder([sse("event: x\ndata: 1\n\n")])
    async with async_client(rec) as c:
        async with c.stream("get", "/s", event_type=str) as ev:
            assert [e.event async for e in ev.events()] == ["x"]

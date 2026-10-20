from collections.abc import Callable

import httpx2
import pytest

from spec2py_runtime import (
    ApiKeyAuth,
    BasicAuth,
    BearerAuth,
    CursorPagination,
    LinkHeaderPagination,
    OffsetPagination,
    PageNumberPagination,
    PaginationOriginError,
    PaginationStrategy,
)

from .helpers import Item, async_client, client


def items(*ids: str) -> list[dict[str, str]]:
    return [{"id": i} for i in ids]


def cursor_api(request: httpx2.Request) -> httpx2.Response:
    pages = {
        None: (items("a", "b"), "c1"),
        "c1": (items("c"), "c2"),
        "c2": ([], None),
    }
    data, nxt = pages[request.url.params.get("cursor")]
    return httpx2.Response(200, json={"data": data, "meta": {"next": nxt}})


def test_cursor_pagination_iterates_lazily_across_pages() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return cursor_api(request)

    strategy = CursorPagination(items="data", next_cursor_path="meta.next")
    page = client(handler).get_api_list(
        "get", "/items", item_type=Item, strategy=strategy, query={"limit": 2}
    )
    assert [i.id for i in page.data] == ["a", "b"]
    assert page.next_cursor == "c1"
    assert len(requests) == 1
    assert [i.id for i in page] == ["a", "b", "c"]
    assert [r.url.params.get("cursor") for r in requests] == [None, "c1", "c2"]
    assert all(r.url.params["limit"] == "2" for r in requests)


def test_manual_page_control() -> None:
    page = client(cursor_api).get_api_list(
        "get",
        "/i",
        item_type=Item,
        strategy=CursorPagination(next_cursor_path="meta.next"),
    )
    assert page.has_next_page()
    second = page.get_next_page()
    assert [i.id for i in second.data] == ["c"]
    pages = list(page.iter_pages())
    assert [len(p.data) for p in pages] == [2, 1, 0]
    assert not pages[-1].has_next_page()
    with pytest.raises(RuntimeError):
        pages[-1].get_next_page()


def test_cursor_from_last_item_with_has_more() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        after = request.url.params.get("starting_after")
        if after is None:
            return httpx2.Response(
                200, json={"data": items("a", "b"), "has_more": True}
            )
        return httpx2.Response(200, json={"data": items("c"), "has_more": False})

    strategy = CursorPagination(
        cursor_param="starting_after",
        next_cursor_path=None,
        cursor_from_item="id",
        has_more="has_more",
    )
    page = client(handler).get_api_list("get", "/i", item_type=Item, strategy=strategy)
    assert [i.id for i in page] == ["a", "b", "c"]


def test_cursor_that_is_a_url() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/page2"):
            return httpx2.Response(200, json={"results": items("z"), "next": None})
        return httpx2.Response(
            200, json={"results": items("y"), "next": "https://api.test/v1/page2"}
        )

    strategy = CursorPagination(items="results", next_cursor_path="next")
    page = client(handler).get_api_list("get", "/i", item_type=Item, strategy=strategy)
    assert [i.id for i in page] == ["y", "z"]


def test_offset_pagination_with_total() -> None:
    data = [{"id": str(i)} for i in range(5)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx2.Response(
            200, json={"items": data[offset : offset + limit], "total": 5}
        )

    strategy = OffsetPagination(items="items", total="total")
    page = client(handler).get_api_list(
        "get", "/i", item_type=Item, strategy=strategy, query={"limit": 2}
    )
    assert [i.id for i in page] == ["0", "1", "2", "3", "4"]


def test_offset_pagination_stops_on_short_page() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url.params.get("offset")))
        return httpx2.Response(200, json={"items": items("a")})

    strategy = OffsetPagination(items="items")
    page = client(handler).get_api_list(
        "get", "/i", item_type=Item, strategy=strategy, query={"limit": 2}
    )
    assert [i.id for i in page] == ["a"]
    assert calls == ["None"]


def test_page_number_pagination() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params.get("page", 1))
        return httpx2.Response(
            200, json={"results": items(f"p{page}"), "total_pages": 3}
        )

    strategy = PageNumberPagination(items="results", total_pages="total_pages")
    page = client(handler).get_api_list("get", "/i", item_type=Item, strategy=strategy)
    assert [i.id for i in page] == ["p1", "p2", "p3"]


def test_link_header_pagination_with_top_level_array() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("page") == "2":
            return httpx2.Response(200, json=items("b"))
        link = '<https://api.test/v1/i?page=2>; rel="next", <https://api.test/v1/i?page=2>; rel="last"'
        return httpx2.Response(200, json=items("a"), headers={"Link": link})

    page = client(handler).get_api_list(
        "get", "/i", item_type=Item, strategy=LinkHeaderPagination()
    )
    assert page.next_cursor == "https://api.test/v1/i?page=2"
    assert [i.id for i in page] == ["a", "b"]


async def test_async_paginator_await_and_iterate() -> None:
    strategy = CursorPagination(next_cursor_path="meta.next")
    async with async_client(cursor_api) as c:
        first = await c.get_api_list("get", "/i", item_type=Item, strategy=strategy)
        assert [i.id for i in first.data] == ["a", "b"]
        second = await first.get_next_page()
        assert [i.id for i in second.data] == ["c"]
        everything = [
            i.id
            async for i in c.get_api_list(
                "get", "/i", item_type=Item, strategy=strategy
            )
        ]
        assert everything == ["a", "b", "c"]
        pages = [p async for p in first.iter_pages()]
        assert len(pages) == 3


# -- next-page URLs must stay on the API's origin ------------------------------

SECRET = "s3cr3t"


def foreign_next(source: str, url: str) -> Callable[[httpx2.Request], httpx2.Response]:
    """An API whose first page points (via Link header or body cursor) at ``url``."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "api.test":
            pytest.fail(f"request leaked to {request.url}: {request.headers}")
        if "page=2" in str(request.url):
            if source == "link":
                return httpx2.Response(200, json=items("b"))
            return httpx2.Response(200, json={"results": items("b"), "next": None})
        if source == "link":
            return httpx2.Response(
                200, json=items("a"), headers={"Link": f'<{url}>; rel="next"'}
            )
        return httpx2.Response(200, json={"results": items("a"), "next": url})

    return handler


def strategy_for(source: str) -> PaginationStrategy:
    if source == "link":
        return LinkHeaderPagination()
    return CursorPagination(items="results", next_cursor_path="next")


CREDENTIALS: dict[str, dict[str, object]] = {
    "bearer": {"auth": BearerAuth(SECRET)},
    "basic": {"auth": BasicAuth("user", SECRET)},
    "api_key_header": {"auth": ApiKeyAuth(SECRET, name="X-API-Key")},
    "api_key_query": {"auth": ApiKeyAuth(SECRET, name="api_key", location="query")},
    "api_key_cookie": {"auth": ApiKeyAuth(SECRET, name="sid", location="cookie")},
    "default_headers": {"default_headers": {"Authorization": f"Token {SECRET}"}},
    "default_query": {"default_query": {"key": SECRET}},
}


@pytest.mark.parametrize("source", ["link", "cursor"])
@pytest.mark.parametrize("creds", CREDENTIALS)
@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/items?page=2",
        "//other.example/items?page=2",
        "http://api.test/v1/items?page=2",  # scheme downgrade
        "https://api.test:8443/v1/items?page=2",
    ],
)
def test_cross_origin_next_url_is_refused(source: str, creds: str, url: str) -> None:
    c = client(foreign_next(source, url), **CREDENTIALS[creds])
    page = c.get_api_list(
        "get",
        "/items",
        item_type=Item,
        strategy=strategy_for(source),
        security=True,
        headers={"X-Param-Key": SECRET},
        cookies={"session": SECRET},
    )
    with pytest.raises(PaginationOriginError) as exc:
        list(page)
    assert exc.value.url == url
    assert exc.value.request.url.host == "api.test"


@pytest.mark.parametrize("source", ["link", "cursor"])
def test_same_origin_and_relative_next_urls_are_followed(source: str) -> None:
    for url in ("https://api.test/v1/items?page=2", "/v1/items?page=2"):
        c = client(foreign_next(source, url), auth=BearerAuth(SECRET))
        page = c.get_api_list(
            "get",
            "/items",
            item_type=Item,
            strategy=strategy_for(source),
            security=True,
        )
        assert [i.id for i in page] == ["a", "b"]


def test_trusted_origin_is_followed_with_credentials() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.host == "cdn.example":
            return httpx2.Response(200, json=items("b"))
        link = '<https://CDN.example:443/items?page=2>; rel="next"'
        return httpx2.Response(200, json=items("a"), headers={"Link": link})

    c = client(
        handler,
        auth=BearerAuth(SECRET),
        trusted_pagination_origins=["https://cdn.example"],
    )
    page = c.get_api_list(
        "get", "/items", item_type=Item, strategy=LinkHeaderPagination(), security=True
    )
    assert [i.id for i in page] == ["a", "b"]
    assert seen[1].headers["Authorization"] == f"Bearer {SECRET}"


def test_trusting_https_does_not_allow_http() -> None:
    url = "http://cdn.example/items?page=2"
    c = client(
        foreign_next("link", url), trusted_pagination_origins=["https://cdn.example"]
    )
    page = c.get_api_list(
        "get", "/items", item_type=Item, strategy=LinkHeaderPagination()
    )
    with pytest.raises(PaginationOriginError):
        page.get_next_page()


@pytest.mark.parametrize("bad", ["cdn.example", "ftp://cdn.example", "/items"])
def test_trusted_origins_are_validated(bad: str) -> None:
    with pytest.raises(ValueError, match="trusted pagination origin"):
        client(cursor_api, trusted_pagination_origins=[bad])


async def test_async_cross_origin_next_url_is_refused() -> None:
    url = "https://other.example/items?page=2"
    async with async_client(foreign_next("link", url), auth=BearerAuth(SECRET)) as c:
        first = await c.get_api_list(
            "get",
            "/items",
            item_type=Item,
            strategy=LinkHeaderPagination(),
            security=True,
        )
        with pytest.raises(PaginationOriginError):
            await first.get_next_page()
        with pytest.raises(PaginationOriginError):
            [
                i
                async for i in c.get_api_list(
                    "get", "/items", item_type=Item, strategy=LinkHeaderPagination()
                )
            ]

"""Pagination strategies and page objects that iterate lazily across pages.

::

    for invoice in client.invoices.list(status="open"):  # every item, every page
        ...

    page = client.invoices.list()
    page.data, page.next_cursor  # one page, for manual control
    if page.has_next_page():
        page = page.get_next_page()
"""

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import httpx2

__all__ = [
    "AsyncPage",
    "AsyncPaginator",
    "CursorPagination",
    "LinkHeaderPagination",
    "NextPage",
    "OffsetPagination",
    "Page",
    "PageNumberPagination",
    "PageState",
    "PaginationStrategy",
    "dig",
]


def dig(body: object, path: str | None) -> object:
    """Look up a dotted path (``"meta.next_cursor"``) in decoded JSON."""
    if not path:
        return body
    current = body
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = cast(dict[str, object], current).get(part)
    return current


@dataclass(frozen=True, slots=True)
class PageState:
    """What a strategy sees about the page just fetched."""

    params: dict[str, object]
    body: object
    headers: httpx2.Headers
    items: list[object]


@dataclass(frozen=True, slots=True)
class NextPage:
    """How to fetch the next page: query overrides, or a whole new URL."""

    params: dict[str, object] = field(default_factory=dict[str, object])
    url: str | None = None


class PaginationStrategy(Protocol):
    @property
    def items(self) -> str | None:
        """Dotted path to the item array in the body (``None``: the body is the array)."""
        ...

    def next_page(self, state: PageState) -> NextPage | None: ...

    def next_cursor(self, state: PageState) -> str | None: ...


def _more(state: PageState, has_more: str | None) -> bool:
    if not state.items:
        return False
    return not (has_more and dig(state.body, has_more) is False)


@dataclass(frozen=True, slots=True)
class CursorPagination:
    """Opaque cursors: ``?cursor=`` from a body field or from the last item's id."""

    items: str | None = "data"
    cursor_param: str = "cursor"
    next_cursor_path: str | None = "next_cursor"
    has_more: str | None = None
    cursor_from_item: str | None = None

    def next_cursor(self, state: PageState) -> str | None:
        if self.cursor_from_item:
            if not state.items:
                return None
            value = dig(state.items[-1], self.cursor_from_item)
        else:
            value = dig(state.body, self.next_cursor_path)
        return None if value in (None, "") else str(value)

    def next_page(self, state: PageState) -> NextPage | None:
        if self.has_more and dig(state.body, self.has_more) is False:
            return None
        cursor = self.next_cursor(state)
        if cursor is None:
            return None
        if cursor.startswith(("http://", "https://", "/")):
            return NextPage(url=cursor)
        return NextPage(params={self.cursor_param: cursor})


@dataclass(frozen=True, slots=True)
class OffsetPagination:
    """``?offset=&limit=`` paging."""

    items: str | None = "data"
    offset_param: str = "offset"
    limit_param: str | None = "limit"
    total: str | None = None
    has_more: str | None = None

    def next_cursor(self, state: PageState) -> str | None:
        return None

    def next_page(self, state: PageState) -> NextPage | None:
        if not _more(state, self.has_more):
            return None
        offset = int(cast(Any, state.params.get(self.offset_param)) or 0)
        next_offset = offset + len(state.items)
        total = dig(state.body, self.total) if self.total else None
        if isinstance(total, int) and next_offset >= total:
            return None
        limit = state.params.get(self.limit_param) if self.limit_param else None
        if total is None and isinstance(limit, int) and len(state.items) < limit:
            return None
        return NextPage(params={self.offset_param: next_offset})


@dataclass(frozen=True, slots=True)
class PageNumberPagination:
    """``?page=N`` paging, counting from ``start``."""

    items: str | None = "data"
    page_param: str = "page"
    total_pages: str | None = None
    has_more: str | None = None
    start: int = 1

    def next_cursor(self, state: PageState) -> str | None:
        return None

    def next_page(self, state: PageState) -> NextPage | None:
        if not _more(state, self.has_more):
            return None
        page = int(cast(Any, state.params.get(self.page_param)) or self.start)
        total_pages = dig(state.body, self.total_pages) if self.total_pages else None
        if isinstance(total_pages, int) and page + 1 - self.start >= total_pages:
            return None
        return NextPage(params={self.page_param: page + 1})


_LINK_RE = re.compile(r"<([^>]*)>\s*((?:;\s*[^;,]+)*)")
_REL_NEXT_RE = re.compile(r'rel\s*=\s*"?([^";]*\s)?next(\s[^";]*)?"?')


@dataclass(frozen=True, slots=True)
class LinkHeaderPagination:
    """RFC 8288 ``Link: <...>; rel="next"`` paging (GitHub style)."""

    items: str | None = None

    def next_cursor(self, state: PageState) -> str | None:
        link = state.headers.get("link")
        if not link:
            return None
        for url, params in _LINK_RE.findall(link):
            if _REL_NEXT_RE.search(params):
                return str(url)
        return None

    def next_page(self, state: PageState) -> NextPage | None:
        url = self.next_cursor(state)
        return NextPage(url=url) if url and state.items else None


class _BasePage[T]:
    def __init__(
        self,
        *,
        data: list[T],
        response: httpx2.Response,
        state: PageState,
        strategy: PaginationStrategy,
    ) -> None:
        self.data = data
        self.response = response
        self._state = state
        self._strategy = strategy

    @property
    def next_cursor(self) -> str | None:
        """The cursor (or next URL) for the following page, if any."""
        return self._strategy.next_cursor(self._state)

    def has_next_page(self) -> bool:
        return self._strategy.next_page(self._state) is not None

    def _next(self) -> NextPage:
        next_page = self._strategy.next_page(self._state)
        if next_page is None:
            raise RuntimeError("No next page; check has_next_page() first.")
        return next_page

    def __repr__(self) -> str:
        return f"{type(self).__name__}(data={self.data!r})"


class Page[T](_BasePage[T]):
    """One page of results; iterating it walks every item on every later page."""

    def __init__(
        self,
        *,
        data: list[T],
        response: httpx2.Response,
        state: PageState,
        strategy: PaginationStrategy,
        fetch: Callable[[NextPage], Page[T]],
    ) -> None:
        super().__init__(data=data, response=response, state=state, strategy=strategy)
        self._fetch = fetch

    def get_next_page(self) -> Page[T]:
        return self._fetch(self._next())

    def iter_pages(self) -> Iterator[Page[T]]:
        page = self
        yield page
        while page.has_next_page():
            page = page.get_next_page()
            yield page

    def __iter__(self) -> Iterator[T]:
        for page in self.iter_pages():
            yield from page.data


class AsyncPage[T](_BasePage[T]):
    """One page of results; ``async for`` walks every item on every later page."""

    def __init__(
        self,
        *,
        data: list[T],
        response: httpx2.Response,
        state: PageState,
        strategy: PaginationStrategy,
        fetch: Callable[[NextPage], Awaitable[AsyncPage[T]]],
    ) -> None:
        super().__init__(data=data, response=response, state=state, strategy=strategy)
        self._fetch = fetch

    async def get_next_page(self) -> AsyncPage[T]:
        return await self._fetch(self._next())

    async def iter_pages(self) -> AsyncIterator[AsyncPage[T]]:
        page = self
        yield page
        while page.has_next_page():
            page = await page.get_next_page()
            yield page

    async def __aiter__(self) -> AsyncIterator[T]:
        async for page in self.iter_pages():
            for item in page.data:
                yield item


class AsyncPaginator[T]:
    """Returned by async list methods: ``await`` it for the first page, or ``async for``
    over it for every item."""

    def __init__(self, fetch_first: Callable[[], Awaitable[AsyncPage[T]]]) -> None:
        self._fetch_first = fetch_first

    def __await__(self) -> Generator[Any, None, AsyncPage[T]]:
        return self._fetch_first().__await__()

    async def __aiter__(self) -> AsyncIterator[T]:
        page = await self._fetch_first()
        async for item in page:
            yield item

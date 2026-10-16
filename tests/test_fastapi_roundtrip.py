"""One command from a FastAPI app to a working client: ``spec2py fastapi module:app``."""

import pathlib
from types import ModuleType
from typing import Any

import httpx2
import pytest

from spec2py.cli import main
from tests.fastapi_app import app as app_module

from .conftest import import_generated


@pytest.fixture(scope="module")
def todo(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    out = tmp_path_factory.mktemp("todo")
    code = main(
        ["fastapi", "tests.fastapi_app.app:app", "-o", str(out), "--layout", "package"]
    )
    assert code == 0
    notes = (out / "todo_service" / "NOTES.md").read_text()
    assert "CursorPagination" in notes and "| `jobs.run` | sse | `Progress` |" in notes
    return import_generated(out, "todo_service")


@pytest.fixture
def client(todo: ModuleType) -> Any:
    app_module._reset()
    return todo.AsyncTodoService(
        api_key="secret",
        base_url="http://testserver",
        transport=httpx2.ASGITransport(app=app_module.app),
    )


async def test_round_trip_crud_pagination_and_errors(
    todo: ModuleType, client: Any
) -> None:
    async with client:
        titles = [t.title async for t in client.todos.list(limit=2)]
        assert titles == [f"todo {i}" for i in range(1, 6)]
        page = await client.todos.list()
        assert len(page.data) == 2 and page.next_cursor == "3"

        created = await client.todos.create(
            title="ship it", priority="high", tags=["release"]
        )
        assert isinstance(created, todo.models.Todo)
        assert (created.id, created.priority, created.done) == (6, "high", False)
        assert (await client.todos.retrieve(6)).title == "ship it"

        await client.todos.delete(6)
        with pytest.raises(todo.NotFoundError) as info:
            await client.todos.retrieve(6)
        assert info.value.message == "todo 6 not found"

        with pytest.raises(todo.UnprocessableEntityError) as invalid:
            await client.todos.create(title="x", extra_body={"priority": "urgent"})
        assert isinstance(invalid.value.parsed, todo.models.HttpValidationError)


async def test_round_trip_auth_upload_and_stream(
    todo: ModuleType, client: Any, tmp_path: pathlib.Path
) -> None:
    async with client:
        attachment = await client.todos.attach_file(
            1, file=("notes.txt", b"hello"), note="see this"
        )
        assert (attachment.filename, attachment.size, attachment.note) == (
            "notes.txt",
            5,
            "see this",
        )

        async with client.jobs.run("build") as events:
            steps = [e async for e in events]
        assert [s.step for s in steps] == [1, 2, 3]
        assert steps[0].message == "build step 1"

        assert await client.meta.health() == {"status": "ok"}

    bad = todo.AsyncTodoService(
        api_key="wrong",
        base_url="http://testserver",
        transport=httpx2.ASGITransport(app=app_module.app),
    )
    async with bad:
        with pytest.raises(todo.AuthenticationError):
            await bad.todos.list()


def test_base_url_is_required_without_servers(
    todo: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TODO_SERVICE_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="TODO_SERVICE_BASE_URL"):
        todo.TodoService(api_key="x")
    monkeypatch.setenv("TODO_SERVICE_BASE_URL", "http://env")
    assert todo.TodoService(api_key="x").base_url == "http://env"

"""A small FastAPI service used for the one-command round-trip test.

``spec2py fastapi tests.fastapi_app.app:app`` must produce a client that can
call this exact app (through an ASGI transport) with every feature working.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

app = FastAPI(title="Todo Service", version="1.2.0")
bearer = HTTPBearer()


def authed(creds: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]) -> str:
    if creds.credentials != "secret":
        raise HTTPException(status_code=401, detail="bad token")
    return creds.credentials


class TodoIn(BaseModel):
    title: str
    priority: Literal["low", "high"] = "low"
    tags: list[str] = Field(default_factory=list)


class Todo(TodoIn):
    id: int
    done: bool = False
    created_at: datetime


class TodoPage(BaseModel):
    data: list[Todo]
    next_cursor: str | None = None


class Progress(BaseModel):
    step: int
    message: str


class Attachment(BaseModel):
    todo_id: int
    filename: str
    size: int
    note: str | None = None


TODOS: dict[int, Todo] = {}


def _reset() -> None:
    TODOS.clear()
    for i in range(1, 6):
        TODOS[i] = Todo(
            id=i, title=f"todo {i}", created_at=datetime(2026, 1, i, tzinfo=UTC)
        )


_reset()


@app.get("/todos", tags=["todos"], operation_id="listTodos")
def list_todos(
    _: Annotated[str, Depends(authed)],
    cursor: str | None = None,
    limit: Annotated[int, Query(le=100)] = 2,
) -> TodoPage:
    ids = sorted(TODOS)
    start = ids.index(int(cursor)) if cursor else 0
    chunk = ids[start : start + limit]
    nxt = ids[start + limit] if start + limit < len(ids) else None
    return TodoPage(
        data=[TODOS[i] for i in chunk], next_cursor=str(nxt) if nxt else None
    )


@app.post("/todos", tags=["todos"], operation_id="createTodo", status_code=201)
def create_todo(body: TodoIn, _: Annotated[str, Depends(authed)]) -> Todo:
    todo = Todo(
        id=max(TODOS, default=0) + 1, created_at=datetime.now(UTC), **body.model_dump()
    )
    TODOS[todo.id] = todo
    return todo


@app.get("/todos/{todo_id}", tags=["todos"], operation_id="getTodo")
def get_todo(todo_id: int, _: Annotated[str, Depends(authed)]) -> Todo:
    if todo_id not in TODOS:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")
    return TODOS[todo_id]


@app.delete(
    "/todos/{todo_id}", tags=["todos"], operation_id="deleteTodo", status_code=204
)
def delete_todo(todo_id: int, _: Annotated[str, Depends(authed)]) -> None:
    TODOS.pop(todo_id, None)


@app.post(
    "/todos/{todo_id}/attachments",
    tags=["todos"],
    operation_id="attachFile",
    status_code=201,
)
async def attach_file(
    todo_id: int,
    _: Annotated[str, Depends(authed)],
    file: Annotated[UploadFile, File()],
    note: Annotated[str | None, Form()] = None,
) -> Attachment:
    content = await file.read()
    return Attachment(
        todo_id=todo_id, filename=file.filename or "", size=len(content), note=note
    )


@app.post(
    "/jobs/{job_id}/run",
    tags=["jobs"],
    operation_id="runJob",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Progress events.",
            "content": {
                "text/event-stream": {
                    "itemSchema": {"$ref": "#/components/schemas/Progress"}
                }
            },
        }
    },
)
async def run_job(job_id: str, _: Annotated[str, Depends(authed)]) -> StreamingResponse:
    async def events() -> AsyncIterator[bytes]:
        for step in range(1, 4):
            payload = Progress(step=step, message=f"{job_id} step {step}")
            yield f"data: {json.dumps(payload.model_dump())}\n\n".encode()

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/health", tags=["meta"], operation_id="health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Make sure Progress shows up in components even though no route returns it.
_original_openapi = app.openapi


def _openapi() -> dict[str, object]:
    if app.openapi_schema is None:
        schema = _original_openapi()
        schema.setdefault("components", {}).setdefault("schemas", {})["Progress"] = (
            Progress.model_json_schema()
        )
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi  # type: ignore[method-assign]

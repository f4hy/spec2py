import datetime
import enum
import io
import pathlib

import httpx2

from apismith_runtime import (
    NOT_GIVEN,
    ApiKeyAuth,
    BasicAuth,
    BearerAuth,
    MultiAuth,
    OAuth2ClientCredentials,
    path_template,
)
from apismith_runtime._files import to_httpx_files, to_multipart_data
from apismith_runtime._qs import encode_query, query_param

from .helpers import Item, Recorder, async_client, client, ok


def test_api_key_locations() -> None:
    rec = Recorder([ok({"id": "1"})])
    c = client(
        rec,
        auth=MultiAuth(
            [
                ApiKeyAuth("h", name="X-Key"),
                ApiKeyAuth("q", name="key", location="query"),
                ApiKeyAuth("c", name="session", location="cookie"),
                ApiKeyAuth("t", name="Authorization", prefix="Token"),
            ]
        ),
    )
    c.request("get", "/x", cast_to=Item)
    r = rec.requests[0]
    assert r.headers["x-key"] == "h"
    assert r.url.params["key"] == "q"
    assert r.headers["cookie"] == "session=c"
    assert r.headers["authorization"] == "Token t"


def test_bearer_and_basic() -> None:
    rec = Recorder([ok({"id": "1"})])
    client(rec, auth=MultiAuth([BearerAuth("b"), BasicAuth("u", "p")])).request(
        "get", "/x", cast_to=Item
    )
    # basic is applied last, so it wins the Authorization header
    assert rec.requests[0].headers["authorization"] == "Basic dTpw"


def oauth_server() -> tuple[Recorder, list[str]]:
    issued: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/oauth/token":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            token = f"tok{len(issued) + 1}"
            issued.append(form["grant_type"])
            return httpx2.Response(
                200,
                json={"access_token": token, "expires_in": 3600, "refresh_token": "r"},
            )
        if request.headers.get(
            "authorization"
        ) == "Bearer tok1" and request.url.path.endswith("/stale"):
            return httpx2.Response(401, json={"message": "expired"})
        return httpx2.Response(200, json={"id": request.headers["authorization"]})

    return Recorder([handler]), issued


def test_oauth2_client_credentials_fetches_caches_and_refreshes_on_401() -> None:
    rec, issued = oauth_server()
    auth = OAuth2ClientCredentials(
        token_url="https://api.test/oauth/token",
        client_id="id",
        client_secret="secret",
        scopes=("read", "write"),
    )
    c = client(rec, auth=auth)
    assert c.request("get", "/a", cast_to=Item).id == "Bearer tok1"
    assert c.request("get", "/b", cast_to=Item).id == "Bearer tok1"
    assert issued == ["client_credentials"]
    assert c.request("get", "/stale", cast_to=Item).id == "Bearer tok2"
    assert issued == ["client_credentials", "client_credentials"]
    token_request = next(r for r in rec.requests if r.url.path == "/oauth/token")
    assert b"scope=read+write" in token_request.content


def test_oauth2_uses_refresh_token_when_expired() -> None:
    rec, issued = oauth_server()
    auth = OAuth2ClientCredentials(
        token_url="https://api.test/oauth/token",
        client_id="id",
        client_secret="s",
        leeway=10_000,
    )
    c = client(rec, auth=auth)
    c.request("get", "/a", cast_to=Item)
    c.request(
        "get", "/b", cast_to=Item
    )  # token "expires" immediately because of leeway
    assert issued == ["client_credentials", "refresh_token"]


async def test_oauth2_works_with_async_client() -> None:
    rec, issued = oauth_server()
    auth = OAuth2ClientCredentials(
        token_url="https://api.test/oauth/token", client_id="id", client_secret="s"
    )
    async with async_client(rec, auth=auth) as c:
        assert (await c.request("get", "/a", cast_to=Item)).id == "Bearer tok1"
    assert issued == ["client_credentials"]


def test_multi_auth_sends_oauth_token_request_without_other_credentials() -> None:
    rec, _ = oauth_server()
    auth = MultiAuth(
        [
            OAuth2ClientCredentials(
                token_url="https://api.test/oauth/token",
                client_id="i",
                client_secret="s",
            ),
            ApiKeyAuth("k", name="X-Key"),
        ]
    )
    client(rec, auth=auth).request("get", "/a", cast_to=Item)
    token_req, api_req = rec.requests
    assert "x-key" not in token_req.headers
    assert api_req.headers["x-key"] == "k"
    assert api_req.headers["authorization"] == "Bearer tok1"


# -- uploads -----------------------------------------------------------------------


def test_files_from_bytes_paths_file_objects_and_tuples(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "report.csv"
    path.write_bytes(b"a,b")
    buf = io.BytesIO(b"xyz")
    buf.name = "/some/dir/data.bin"
    files = to_httpx_files(
        {
            "raw": b"123",
            "path": path,
            "obj": buf,
            "named": ("custom.txt", b"t", "text/plain"),
            "many": [b"1", b"2"],
            "skip": NOT_GIVEN,
        }
    )
    assert files[0] == ("raw", ("upload", b"123", None))
    assert files[1] == ("path", ("report.csv", b"a,b", None))
    assert files[2][1][0] == "data.bin"
    assert files[3] == ("named", ("custom.txt", b"t", "text/plain"))
    assert [f[0] for f in files[4:]] == ["many", "many"]


def test_multipart_form_fields() -> None:
    data = to_multipart_data(
        {
            "s": "x",
            "n": 3,
            "b": False,
            "tags": ["a", 1],
            "meta": {"k": "v"},
            "skip": NOT_GIVEN,
            "none": None,
        }
    )
    assert data == {
        "s": "x",
        "n": "3",
        "b": "false",
        "tags": ["a", "1"],
        "meta": '{"k": "v"}',
    }


def test_multipart_request_end_to_end() -> None:
    rec = Recorder([ok({"id": "1"})])
    client(rec).request(
        "post",
        "/upload",
        cast_to=Item,
        files={"file": ("a.txt", b"hello")},
        form={"purpose": "x"},
    )
    req = rec.requests[0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"; filename="a.txt"' in req.content
    assert b'name="purpose"' in req.content


def test_raw_content_body() -> None:
    rec = Recorder([ok({"id": "1"})])
    client(rec).request(
        "put", "/blob", cast_to=Item, content=b"\x00", content_type="image/png"
    )
    assert rec.requests[0].headers["content-type"] == "image/png"
    assert rec.requests[0].content == b"\x00"


# -- query and path encoding --------------------------------------------------------


class Color(enum.Enum):
    RED = "red"


def test_query_styles() -> None:
    assert query_param("ids", [1, 2]) == [("ids", "1"), ("ids", "2")]
    assert query_param("ids", [1, 2], explode=False) == [("ids", "1,2")]
    assert query_param("ids", [1, 2], style="pipeDelimited") == [("ids", "1|2")]
    assert query_param("ids", [1, 2], style="spaceDelimited") == [("ids", "1 2")]
    assert query_param("f", {"a": 1, "b": True}, style="deepObject") == [
        ("f[a]", "1"),
        ("f[b]", "true"),
    ]
    assert query_param("f", {"a": 1}, explode=False) == [("f", "a,1")]
    assert query_param("c", Color.RED) == [("c", "red")]
    when = datetime.datetime(2026, 1, 2, 3, 4, tzinfo=datetime.UTC)
    assert query_param("t", when) == [("t", "2026-01-02T03:04:00+00:00")]
    assert query_param("x", NOT_GIVEN) == []
    assert encode_query({"a": [1, 2], "b": "x"}, {"a": ("form", False)}) == [
        ("a", "1,2"),
        ("b", "x"),
    ]


def test_path_template_encodes_values() -> None:
    assert path_template("/a/{x}/b/{y}", x="a/b c", y=[1, 2]) == "/a/a%2Fb%20c/b/1%2C2"

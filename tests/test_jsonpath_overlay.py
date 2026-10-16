import copy
from typing import Any

import pytest

from spec2py.jsonpath import JSONPathError, find
from spec2py.overlay import OverlayError, apply_overlay

DOC: dict[str, Any] = {
    "paths": {
        "/a": {"get": {"operationId": "getA", "tags": ["x"]}, "post": {"tags": ["y"]}},
        "/b/{id}": {"get": {"operationId": "getB", "deprecated": True}},
    },
    "items": [{"n": 1, "name": "one"}, {"n": 2, "name": "two"}, {"n": 3}],
    "weird key": {"it's": 1},
}


def values(path: str) -> list[Any]:
    return [loc.value for loc in find(DOC, path)]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", [DOC]),
        ("$.paths['/a'].get.operationId", ["getA"]),
        ('$.paths["/b/{id}"].get.operationId', ["getB"]),
        ("$.items[0].n", [1]),
        ("$.items[-1].n", [3]),
        ("$.items[0:2].n", [1, 2]),
        ("$.items[::2].n", [1, 3]),
        ("$.items[*].name", ["one", "two"]),
        ("$.items[0,2].n", [1, 3]),
        ("$..operationId", ["getA", "getB"]),
        ("$.paths.*.*.operationId", ["getA", "getB"]),
        ("$.items[?@.n > 1].n", [2, 3]),
        ("$.items[?(@.n >= 2 && @.name)].name", ["two"]),
        ("$.items[?@.n == 1 || @.n == 3].n", [1, 3]),
        ("$.items[?!@.name].n", [3]),
        ("$.paths.*[?@.operationId == 'getB'].deprecated", [True]),
        ("$.paths.*.*[?(@.tags)]", []),
        ("$.paths.*[?count(@.tags) == 1].tags[0]", ["x", "y"]),
        ("$.items[?length(@.name) == 3].name", ["one", "two"]),
        ("$.items[?match(@.name, 't.*')].name", ["two"]),
        ("$.items[?search(@.name, 'n')].name", ["one"]),
        ("$['weird key']['it\\'s']", [1]),
        ("$..[?@.operationId == 'getA'].tags[0]", ["x"]),
    ],
)
def test_jsonpath(path: str, expected: list[Any]) -> None:
    assert values(path) == expected


def test_jsonpath_locations_are_mutable() -> None:
    doc = copy.deepcopy(DOC)
    for loc in find(doc, "$.items[*].n"):
        loc.set(loc.value * 10)
    assert [i["n"] for i in doc["items"]] == [10, 20, 30]
    assert find(doc, "$.items[1]")[0].path == ("items", 1)


@pytest.mark.parametrize(
    "bad", ["paths", "$.", "$[", "$.items[?@.n ==]", "$.x[?nope(@)]", "$ $"]
)
def test_jsonpath_errors(bad: str) -> None:
    with pytest.raises(JSONPathError):
        find(DOC, bad)


def test_overlay_update_remove_copy() -> None:
    doc = copy.deepcopy(DOC)
    report = apply_overlay(
        doc,
        {
            "overlay": "1.1.0",
            "actions": [
                {
                    "target": "$.paths['/a'].post",
                    "update": {"operationId": "createA", "tags": ["z"]},
                },
                {"target": "$.paths['/b/{id}']", "remove": True},
                {"target": "$.items[?@.n == 3]", "copy": "$.items[0]"},
                {"target": "$", "update": {"info": {"title": "T"}}},
                {"target": "$.missing", "update": {"x": 1}},
                {"target": "$.items[?@.n == 2]", "remove": True},
            ],
        },
    )
    # nested arrays are replaced; target the array itself to append
    assert doc["paths"]["/a"]["post"] == {"tags": ["z"], "operationId": "createA"}
    assert "/b/{id}" not in doc["paths"]
    # copy merged items[0] into the n == 3 item, then n == 2 was removed
    assert doc["items"] == [{"n": 1, "name": "one"}, {"n": 1, "name": "one"}]
    assert doc["info"] == {"title": "T"}
    assert report.applied == 5
    assert report.unmatched == ["$.missing"]


def test_overlay_errors() -> None:
    with pytest.raises(OverlayError, match="not an Overlay"):
        apply_overlay({}, {"actions": []})
    with pytest.raises(OverlayError, match="no target"):
        apply_overlay({}, {"overlay": "1.0.0", "actions": [{"update": {}}]})
    with pytest.raises(OverlayError, match="action 0"):
        apply_overlay({}, {"overlay": "1.0.0", "actions": [{"target": "nope"}]})

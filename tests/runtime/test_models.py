import sys
import types
from typing import Annotated, Literal

import pytest
from pydantic import Field, ValidationError

from apismith_runtime import (
    NOT_GIVEN,
    BaseModel,
    NotGiven,
    Tolerant,
    is_given,
    jsonable,
    link_models,
    parse_value,
    validate_param,
)

type Status = Annotated[Literal["open", "paid"], Tolerant]


class Invoice(BaseModel):
    id: str
    status: Status
    customer_id: str | None = Field(default=None, alias="customerId")
    memo: str | None = None
    lines: list[Line] = []


class Line(BaseModel):
    amount: int


class Cat(BaseModel):
    type: Literal["cat"] = "cat"
    meows: bool


class Dog(BaseModel):
    type: Literal["dog"] = "dog"
    barks: bool


type Pet = Annotated[Cat | Dog, Field(discriminator="type")]


def test_not_given_sentinel() -> None:
    assert NotGiven() is NOT_GIVEN
    assert not NOT_GIVEN
    assert repr(NOT_GIVEN) == "NOT_GIVEN"
    assert is_given(0) and not is_given(NOT_GIVEN)
    import copy
    import pickle

    assert copy.deepcopy(NOT_GIVEN) is NOT_GIVEN
    assert pickle.loads(pickle.dumps(NOT_GIVEN)) is NOT_GIVEN


def test_lenient_in_strict_out_for_enums() -> None:
    inv = Invoice.model_validate({"id": "1", "status": "refunded", "customerId": "c"})
    assert inv.status == "refunded"
    assert inv.customer_id == "c"
    with pytest.raises(ValidationError):
        validate_param(Status, "refunded")
    assert validate_param(Status, "paid") == "paid"


def test_absent_vs_null() -> None:
    absent = Invoice.model_validate({"id": "1", "status": "open"})
    null = Invoice.model_validate({"id": "1", "status": "open", "memo": None})
    assert absent.memo is None and null.memo is None
    assert "memo" not in absent.model_fields_set
    assert "memo" in null.model_fields_set
    assert absent.to_dict() == {"id": "1", "status": "open"}
    assert null.to_dict() == {"id": "1", "status": "open", "memo": None}


def test_wire_names_on_output_and_python_names_on_input() -> None:
    inv = Invoice(id="1", status="open", customer_id="c")
    assert inv.to_dict() == {"id": "1", "status": "open", "customerId": "c"}
    assert '"customerId":"c"' in inv.to_json()


def test_discriminated_union() -> None:
    pets = parse_value(list[Pet], [{"type": "cat", "meows": True}, {"type": "dog", "barks": False}])
    assert [type(p).__name__ for p in pets] == ["Cat", "Dog"]
    assert Cat(meows=True).type == "cat"


def test_best_effort_parse_keeps_what_validates() -> None:
    inv = parse_value(Invoice, {"id": "1", "status": "open", "lines": [{"amount": 1}, {"amount": "x"}], "new": True})
    assert inv.id == "1"
    assert isinstance(inv.lines[0], Line)
    assert inv.lines[1].amount == "x"  # type: ignore[comparison-overlap]
    assert inv.model_extra == {"new": True}
    with pytest.raises(ValidationError):
        parse_value(Invoice, {"id": 1, "status": "open"}, strict=True)
    items = parse_value(list[Line], [{"amount": 1}, {"amount": "bad"}])
    assert items[0].amount == 1


def test_jsonable() -> None:
    import datetime

    when = datetime.date(2026, 1, 2)
    value = {"inv": Invoice(id="1", status="open"), "d": when, "skip": NOT_GIVEN, "t": (1, 2)}
    assert jsonable(value) == {"inv": {"id": "1", "status": "open"}, "d": "2026-01-02", "t": [1, 2]}


def test_link_models_resolves_cross_module_references() -> None:
    pkg = types.ModuleType("fakepkg.models")
    pkg.__dict__["__name__"] = "fakepkg.models"
    sub = types.ModuleType("fakepkg.models._a")
    sys.modules["fakepkg.models._a"] = sub
    exec(
        "from apismith_runtime import BaseModel\n"
        "class A(BaseModel):\n"
        "    b: B | None = None\n",
        sub.__dict__,
    )
    sub_b = types.ModuleType("fakepkg.models._b")
    sys.modules["fakepkg.models._b"] = sub_b
    exec("from apismith_runtime import BaseModel\nclass B(BaseModel):\n    x: int\n", sub_b.__dict__)
    namespace = {"__name__": "fakepkg.models", "__all__": ["A", "B"], "A": sub.A, "B": sub_b.B}
    try:
        link_models(namespace)
        assert sub.A.model_validate({"b": {"x": 1}}).b.x == 1
    finally:
        del sys.modules["fakepkg.models._a"], sys.modules["fakepkg.models._b"]

"""Bit-exact parity for held portfolio collateral (OSM-002)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from golden_support.registry import default as registry

from app.options.contract import OptionContract
from app.options.requirement import EntrySize, StructureMoneyRefused, portfolio_broker_requirement
from app.options.structure import ResolvedLeg

_INPUTS = json.loads(registry.resolve("OSM-002", "input").read_text(encoding="utf-8"))
_EXPECTED = {
    case["case"]: {key: value for key, value in case.items() if key != "case"}
    for case in json.loads(registry.resolve("OSM-002", "output").read_text(encoding="utf-8"))["cases"]
}


@pytest.mark.parametrize("case", _INPUTS["cases"], ids=[case["case"] for case in _INPUTS["cases"]])
def test_held_portfolio_matches_the_independent_margin_fixture(case: dict[str, Any]) -> None:
    legs = tuple(ResolvedLeg(
        leg_id=leg["leg_id"], position=leg["position"], ratio=leg["ratio"],
        contract=OptionContract.of(
            root=leg["root"], expiry_date=datetime.strptime(leg["expiry"], "%y%m%d").date(),
            right=leg["right"], strike=Decimal(leg["strike"]),
        ),
    ) for leg in case["legs"])
    try:
        actual = {"broker_requirement_cents": portfolio_broker_requirement(legs, EntrySize(case.get("multiplier", 100), 1))}
    except StructureMoneyRefused as exc:
        actual = {"refusal": exc.reason.value}
    # Integer cents: the fixture's tolerance is exactly zero.
    assert actual == _EXPECTED[case["case"]]

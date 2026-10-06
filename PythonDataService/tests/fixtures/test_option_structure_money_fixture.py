"""Golden parity for an option structure's money (OSM-001)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.options.contract import OptionContract  # noqa: E402
from app.options.requirement import (  # noqa: E402
    EntrySize,
    LegQuote,
    StructureMoneyRefused,
    broker_requirement,
    entry_money,
    natural_net_price,
    round_trip_cost,
)
from app.options.structure import ResolvedLeg, ResolvedStructure  # noqa: E402

FIXTURE_ID = "OSM-001"

_INPUTS = json.loads(registry.resolve(FIXTURE_ID, "input").read_text(encoding="utf-8"))
_EXPECTED = {
    case["case"]: case
    for case in json.loads(registry.resolve(FIXTURE_ID, "output").read_text(encoding="utf-8"))["cases"]
}


def _structure(case: dict[str, Any]) -> ResolvedStructure:
    return ResolvedStructure(
        legs=tuple(
            ResolvedLeg(
                leg_id=leg["leg_id"],
                contract=OptionContract.of(
                    root="SPY",
                    expiry_date=datetime.strptime(leg["expiry"], "%y%m%d").date(),
                    right=leg["right"],
                    strike=Decimal(leg["strike"]),
                ),
                position=leg["position"],
                ratio=leg["ratio"],
            )
            for leg in case["legs"]
        )
    )


def _worked_out(case: dict[str, Any]) -> dict[str, Any]:
    """Everything the fixture states for a case, from the canonical functions."""
    structure = _structure(case)
    size = EntrySize(multiplier=_INPUTS["multiplier"], units=case.get("units", 1))
    try:
        actual: dict[str, Any] = {"broker_requirement_cents": broker_requirement(structure, size)}
        quotes = {leg_id: LegQuote(Decimal(bid), Decimal(ask)) for leg_id, (bid, ask) in case.get("quotes", {}).items()}
        if quotes:
            net_price = natural_net_price(structure, quotes)
        elif "leg_prices" in case:
            # A case that states each leg's price states both sides of the market as that price.
            stated = {leg_id: LegQuote(Decimal(price), Decimal(price)) for leg_id, price in case["leg_prices"].items()}
            net_price = natural_net_price(structure, stated)
        else:
            return actual
        money = entry_money(structure, size, net_price=net_price, fee_provision_cents=0)
    except StructureMoneyRefused as refused:
        return {"refusal": refused.reason.value}
    actual |= {
        "net_entry_premium_cents": money.net_entry_premium_cents,
        "commitment_before_fees_cents": money.entry_commitment_cents,
        "loss_at_expiry_cents": money.loss_at_expiry_cents,
    }
    assert money.broker_requirement_cents == actual["broker_requirement_cents"]
    if quotes:
        actual["round_trip_cost_cents"] = round_trip_cost(structure, quotes, size)
    return actual


@pytest.mark.parametrize("case", _INPUTS["cases"], ids=[case["case"] for case in _INPUTS["cases"]])
def test_structure_money_matches_alpacas_examples_and_the_hand_derived_cases(case: dict[str, Any]) -> None:
    expected = {key: value for key, value in _EXPECTED[case["case"]].items() if key != "case"}

    # Whole cents, compared exactly: the fixture's tolerance is zero.
    assert _worked_out(case) == expected

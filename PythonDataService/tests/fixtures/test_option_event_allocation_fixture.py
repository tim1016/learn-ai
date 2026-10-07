"""Golden parity for the shares and cash an exercise, an assignment and an expiry create (OEV-001).

The inputs are the events Alpaca's pages document, and the outputs are the
share trades those pages print beside them: documentation, not recordings.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.options.contract import OptionContract  # noqa: E402
from app.options.event_allocation import (  # noqa: E402
    HeldOptionLot,
    OptionEventAllocation,
    OptionEventKind,
    ShareTrade,
    allocate_option_event,
    expected_share_trade,
)

FIXTURE_ID = "OEV-001"

_INPUTS = json.loads(registry.resolve(FIXTURE_ID, "input").read_text(encoding="utf-8"))
_CASES = {case["case"]: case for case in _INPUTS["cases"]}
_EXPECTED = {
    case["case"]: case
    for case in json.loads(registry.resolve(FIXTURE_ID, "output").read_text(encoding="utf-8"))["cases"]
}
_WITH_A_SHARE_TRADE = sorted(name for name, expected in _EXPECTED.items() if expected["share_trade"] is not None)
_WITH_NONE = sorted(name for name, expected in _EXPECTED.items() if expected["share_trade"] is None)

# Accepted divergence, the only one. The Trading API page's exercised calls
# print "price": "90" on a contract struck at 150. The sentence under that
# example says "a per-share amount of $150 (strike price)", and its own net
# amount, -30000 for 200 shares, is 150 a share. The rule is held to the
# sentence and the net amount; the printed 90 is taken to be the page's slip
# and is pinned here so that it stays the only one.
_PRICE_ALPACA_PRINTS_AGAINST_ITS_OWN_TEXT = {"trading_api/option_exercise": Decimal("90")}


def _contract(name: str) -> OptionContract:
    return OptionContract.from_occ_symbol(_CASES[name]["contract"])


def _expected_share_trade(name: str) -> ShareTrade | None:
    case = _CASES[name]
    return expected_share_trade(
        OptionEventKind(case["kind"]),
        _contract(name),
        contracts=abs(case["position_change"]),
        multiplier=_INPUTS["multiplier"],
    )


def _allocated_to_one_bot(name: str) -> OptionEventAllocation:
    """The event matched to one bot that holds exactly its contracts, on the side its row's sign ends."""
    case = _CASES[name]
    held = HeldOptionLot(
        owner="bot-a",
        contract=_contract(name),
        # The broker's change undoes the position: a bought lot for a negative change, a sold one for a positive.
        quantity=-case["position_change"],
        opened_at_ms=case["event_date_ms"],
        sequence=0,
    )
    return allocate_option_event(
        kind=OptionEventKind(case["kind"]),
        contract=held.contract,
        position_change=case["position_change"],
        event_date_ms=case["event_date_ms"],
        multiplier=_INPUTS["multiplier"],
        lots=[held],
    )


def test_the_fixture_holds_every_example_alpaca_publishes() -> None:
    assert sorted(_CASES) == sorted(_EXPECTED)
    assert _WITH_A_SHARE_TRADE == [
        "broker_api/call_assignment",
        "broker_api/exercising_call",
        "broker_api/exercising_put",
        "broker_api/put_assignment",
        "trading_api/option_assignment",
        "trading_api/option_exercise",
    ]
    assert _WITH_NONE == ["broker_api/call_expiration", "broker_api/put_expiration", "trading_api/otm_option_expiry"]


@pytest.mark.parametrize("name", _WITH_A_SHARE_TRADE)
def test_an_exercise_or_an_assignment_creates_the_share_trade_alpaca_prints_beside_it(name: str) -> None:
    expected: dict[str, Any] = _EXPECTED[name]
    printed = expected["share_trade"]

    trade = _expected_share_trade(name)

    # Whole shares and whole cents against exact decimals: the fixture's tolerance is zero.
    assert isinstance(trade, ShareTrade)
    assert trade.root == printed["symbol"]
    assert Decimal(trade.shares) == Decimal(printed["qty"])
    assert Decimal(trade.cash_cents) == Decimal(printed["net_amount"]) * 100
    assert trade.price == Decimal(expected["price_from_net_amount"])
    if expected["price_in_sentence"] is not None:
        assert trade.price == Decimal(expected["price_in_sentence"])
    assert Decimal(printed["price"]) == _PRICE_ALPACA_PRINTS_AGAINST_ITS_OWN_TEXT.get(name, trade.price)


@pytest.mark.parametrize("name", _WITH_A_SHARE_TRADE)
def test_the_bot_whose_lot_is_taken_gets_those_shares_and_that_cash(name: str) -> None:
    expected: dict[str, Any] = _EXPECTED[name]
    printed = expected["share_trade"]

    allocation = _allocated_to_one_bot(name)

    (lot,) = allocation.lots
    assert (lot.owner, lot.contracts, lot.remaining_quantity) == ("bot-a", abs(_CASES[name]["position_change"]), 0)
    assert Decimal(lot.share_quantity_change) == Decimal(printed["qty"])
    assert Decimal(lot.cash_change_cents) == Decimal(printed["net_amount"]) * 100
    assert allocation.share_price == Decimal(expected["price_from_net_amount"])


@pytest.mark.parametrize("name", _WITH_NONE)
def test_an_expiry_creates_no_share_trade_as_alpacas_examples_print_none(name: str) -> None:
    assert _expected_share_trade(name) is None

    allocation = _allocated_to_one_bot(name)

    (lot,) = allocation.lots
    assert (lot.contracts, lot.remaining_quantity) == (abs(_CASES[name]["position_change"]), 0)
    assert (lot.share_quantity_change, lot.cash_change_cents, allocation.share_price) == (0, 0, None)


def test_the_page_prints_a_price_against_its_own_text_once_and_nowhere_else() -> None:
    """A second example that disagreed with its own net amount would be a new question, not this accepted one."""
    against_its_own_net_amount = {
        name: Decimal(_EXPECTED[name]["share_trade"]["price"])
        for name in _WITH_A_SHARE_TRADE
        if Decimal(_EXPECTED[name]["share_trade"]["price"]) != Decimal(_EXPECTED[name]["price_from_net_amount"])
    }

    assert against_its_own_net_amount == _PRICE_ALPACA_PRINTS_AGAINST_ITS_OWN_TEXT
    # Where the page states the price in a sentence, the sentence and the net amount say the same.
    assert {
        name: (_EXPECTED[name]["price_in_sentence"], _EXPECTED[name]["price_from_net_amount"])
        for name in _WITH_A_SHARE_TRADE
        if _EXPECTED[name]["price_in_sentence"] is not None
    } == {"trading_api/option_assignment": ("150", "150"), "trading_api/option_exercise": ("150", "150")}

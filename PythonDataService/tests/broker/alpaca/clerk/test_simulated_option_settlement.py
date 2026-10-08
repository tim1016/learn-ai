"""What a simulation books an expired contract at: its exercise value, rounded down to the cent (#2913).

The figures are worked out by hand here. The rule is the simulation's own
(ADR 0076 decision 22): a Dry Run has no broker to end its positions, so it
books the sale Alpaca says it makes of an expiring position, in cash, at the
contract's exercise value at the close. Nothing here says what a real
account would have got.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.clerk.simulated_option_settlement import (
    SIMULATED_SETTLEMENT_RULE,
    SettlementPrice,
    SimulatedOutcome,
    SynthesizedOptionSettlement,
    check_settlement_note,
    exercise_value,
    plain,
)
from app.options.contract import OptionContract
from app.options.requirement import exercise_payoff_thousandths
from app.options.structure import ResolvedLeg, ResolvedStructure

CALL = OptionContract.from_occ_symbol("SPY260918C00585000")
PUT = OptionContract.from_occ_symbol("SPY260918P00585000")
CLOSE_MS = 1_789_761_600_000


@pytest.mark.parametrize(
    ("contract", "close", "contracts", "multiplier", "worked_out", "booked", "cash_cents"),
    [
        # 590.25 - 585 = 5.25 a share; one contract of a hundred shares is $525.00.
        pytest.param(CALL, "590.25", 1, 100, "5.25", "5.25", 52_500, id="a call 5.25 in the money"),
        # 585 - 580 = 5.00 a share: $500.00.
        pytest.param(PUT, "580.00", 1, 100, "5", "5", 50_000, id="a put 5.00 in the money"),
        # Three contracts: 3 x 100 x 5.25 = $1,575.00.
        pytest.param(CALL, "590.25", 3, 100, "5.25", "5.25", 157_500, id="three contracts"),
        # A contract of ten shares: 1 x 10 x 5.25 = $52.50.
        pytest.param(CALL, "590.25", 1, 10, "5.25", "5.25", 5_250, id="a multiplier of ten"),
        # 0.12345 a share is booked at 0.12, never 0.13 and never 0.12345: $12.00, not $12.345.
        pytest.param(CALL, "585.12345", 1, 100, "0.12345", "0.12", 1_200, id="rounded down to the cent"),
        pytest.param(CALL, "585.019", 1, 100, "0.019", "0.01", 100, id="a cent and most of another is one cent"),
        pytest.param(CALL, "585.01", 1, 100, "0.01", "0.01", 100, id="a call a cent in the money"),
        pytest.param(PUT, "584.99", 1, 100, "0.01", "0.01", 100, id="a put a cent in the money"),
        # Under a cent rounds down to nothing: an expiry, with no jump between the two outcomes.
        pytest.param(CALL, "585.009", 1, 100, "0.009", "0", 0, id="a call a fraction of a cent in the money"),
        pytest.param(PUT, "584.995", 1, 100, "0.005", "0", 0, id="a put half a cent in the money"),
        pytest.param(CALL, "585.00", 1, 100, "0", "0", 0, id="a call at the money"),
        pytest.param(PUT, "585.00", 1, 100, "0", "0", 0, id="a put at the money"),
        # Out of the money is worth nothing, never less than nothing.
        pytest.param(CALL, "580.00", 1, 100, "0", "0", 0, id="a call out of the money"),
        pytest.param(PUT, "590.25", 1, 100, "0", "0", 0, id="a put out of the money"),
    ],
)
def test_a_contract_is_worth_its_exercise_value_rounded_down_to_the_cent(
    contract: OptionContract, close: str, contracts: int, multiplier: int, worked_out: str, booked: str,
    cash_cents: int,
) -> None:
    worth = exercise_value(contract, underlying_price=Decimal(close), contracts=contracts, multiplier=multiplier)

    assert (worth.worked_out, worth.booked, worth.cash_cents) == (Decimal(worked_out), Decimal(booked), cash_cents)


@pytest.mark.parametrize("contract", [CALL, PUT], ids=["a call", "a put"])
@pytest.mark.parametrize("price_thousandths", [1, 570_000, 584_999, 585_000, 585_001, 585_010, 590_250, 1_200_000])
def test_the_value_worked_out_is_the_exercise_payoff_the_structure_money_uses(
    contract: OptionContract, price_thousandths: int
) -> None:
    """One formula for what a contract pays at expiry. The requirement's own, for one bought leg, exact."""
    bought = ResolvedStructure(legs=(ResolvedLeg(leg_id="leg", contract=contract, position="long", ratio=1),))
    price = Decimal(price_thousandths).scaleb(-3)

    worth = exercise_value(contract, underlying_price=price, contracts=1, multiplier=100)

    assert worth.worked_out * 1000 == exercise_payoff_thousandths(bought, price_thousandths)


@pytest.mark.parametrize(
    "asked",
    [
        pytest.param({"underlying_price": Decimal(0)}, id="a price of nothing"),
        pytest.param({"underlying_price": Decimal("-1")}, id="a price below nothing"),
        pytest.param({"underlying_price": Decimal("NaN")}, id="a price that is no number"),
        pytest.param({"contracts": 0}, id="no contracts"),
        pytest.param({"contracts": True}, id="a contract count that is no number"),
        pytest.param({"multiplier": 0}, id="no multiplier"),
    ],
)
def test_a_value_is_worked_out_only_from_a_price_and_counts_that_can_be(asked: dict[str, Any]) -> None:
    given = {"underlying_price": Decimal("590.25"), "contracts": 1, "multiplier": 100, **asked}

    with pytest.raises(ValueError):
        exercise_value(CALL, **given)


def _price(close: str, *, before_close_ms: int = 0) -> SettlementPrice:
    return SettlementPrice(
        underlying_price=Decimal(close), bar_ref="bar-1", bar_provider="ibkr", bar_end_ms=CLOSE_MS - before_close_ms,
        bar_before_close_ms=before_close_ms,
    )


def _record(**changes: Any) -> SynthesizedOptionSettlement:
    """One call settled from a close of 590.25: 5.25 a share, $525.00, and a fee of nine cents."""
    stated: dict[str, Any] = {
        "contract": CALL, "outcome": SimulatedOutcome.SETTLED_IN_CASH, "contracts": 1, "multiplier": 100,
        "last_session_close_ms": CLOSE_MS, "settled_at_ms": CLOSE_MS + 1, "rule": SIMULATED_SETTLEMENT_RULE,
        "price": _price("590.25"), "exercise_value": Decimal("5.25"), "booked_price": Decimal("5.25"),
        "cash_cents": 52_500, "fee_cents": 9,
    }
    return SynthesizedOptionSettlement(**{**stated, **changes})


def test_a_settlements_record_states_every_figure_and_where_its_price_came_from() -> None:
    said = _record(price=_price("590.25", before_close_ms=60_000)).note()

    assert said == {
        "rule": SIMULATED_SETTLEMENT_RULE, "outcome": "settled_in_cash", "underlying": "SPY",
        "underlying_price": "590.25", "strike": "585", "right": "call", "exercise_value": "5.25",
        "booked_price": "5.25", "contracts": 1, "multiplier": 100, "cash_cents": 52_500, "bar_ref": "bar-1",
        "bar_provider": "ibkr", "bar_end_ms": CLOSE_MS - 60_000, "last_session_close_ms": CLOSE_MS,
        "bar_before_close_ms": 60_000, "settled_at_ms": CLOSE_MS + 1,
        "fees": "Charged by the option fee model, as for every simulated option sale.", "fee_cents": 9,
    }
    assert "simulation" in said["rule"].lower() and "not a broker" in said["rule"].lower()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"booked_price": Decimal("5.26")}, id="a booked price that is not the value rounded down"),
        pytest.param({"exercise_value": Decimal("5.26")}, id="a value its own price does not give"),
        pytest.param({"cash_cents": 52_501}, id="cash a cent out"),
        pytest.param({"cash_cents": 525}, id="cash for one share and not for the contract"),
        pytest.param({"outcome": SimulatedOutcome.EXPIRED, "fee_cents": None}, id="a sale recorded as an expiry"),
        pytest.param(
            {"price": _price("584"), "exercise_value": Decimal(0), "booked_price": Decimal(0), "cash_cents": 0,
             "fee_cents": None},
            id="an expiry recorded as a sale",
        ),
        pytest.param(
            {"price": _price("584"), "exercise_value": Decimal(0), "booked_price": Decimal(0), "cash_cents": 0,
             "outcome": SimulatedOutcome.EXPIRED, "fee_cents": 8},
            id="a fee for an expiry, which is no sale",
        ),
    ],
)
def test_a_record_whose_figures_are_not_its_own_prices_is_no_record(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _record(**changes)


def test_the_books_accept_a_record_whose_figures_give_the_price_booked() -> None:
    check_settlement_note(
        _record().note(), CALL, multiplier=100, outcome="settled_in_cash", close_price=Decimal("5.25")
    )


@pytest.mark.parametrize(
    ("note_changes", "outcome", "close_price"),
    [
        pytest.param({}, "settled_in_cash", "5.26", id="a booking a cent above what the record gives"),
        pytest.param({}, "settled_in_cash", "525", id="a booking at the whole contract's cash"),
        pytest.param({}, "expired", "5.25", id="a sale booked as an expiry"),
        pytest.param({"underlying_price": "600"}, "settled_in_cash", "5.25", id="a price that gives another value"),
        pytest.param({"booked_price": "5.24"}, "settled_in_cash", "5.25", id="a record that booked another price"),
        pytest.param({"cash_cents": 52_499}, "settled_in_cash", "5.25", id="a record whose cash is a cent out"),
        pytest.param({"underlying_price": None}, "settled_in_cash", "5.25", id="a record with no price"),
        pytest.param({"underlying_price": "a lot"}, "settled_in_cash", "5.25", id="a price that is no number"),
        pytest.param({"contracts": 0}, "settled_in_cash", "5.25", id="a record of no contracts"),
    ],
)
def test_the_books_refuse_a_record_that_does_not_give_the_price_booked(
    note_changes: dict[str, Any], outcome: str, close_price: str
) -> None:
    note = {**_record().note(), **note_changes}

    with pytest.raises(ValueError):
        check_settlement_note(note, CALL, multiplier=100, outcome=outcome, close_price=Decimal(close_price))


def test_a_decimal_is_written_out_in_full_with_no_exponent() -> None:
    assert [plain(Decimal(text)) for text in ("585.000", "5.9E+2", "0.10", "0", "590.25")] == [
        "585", "590", "0.1", "0", "590.25"
    ]

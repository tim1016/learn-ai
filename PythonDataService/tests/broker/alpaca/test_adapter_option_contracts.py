"""Alpaca's listing of one option contract → ``BrokerOptionContract``, and whether a bot may trade it.

The listings are Alpaca's published examples
(``tests/fixtures/alpaca_documented/option_contracts``): documentation, not
recordings. Both are a standard active American AAPL call; what a
non-standard listing looks like on the wire is not shown anywhere.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.adapter import from_alpaca_option_contract
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.contract.errors import BrokerEvidenceUnavailable
from app.broker.contract.options import OptionContractRefusal, option_contract_refusal
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms
from tests.broker.alpaca.documented_examples import documented

_OBSERVED = 1_700_000_000_000
_AAPL_JUN_100_CALL = OptionContract.from_occ_symbol("AAPL250620C00100000")


def _example(name: str = "reference_example") -> dict[str, Any]:
    return copy.deepcopy(documented("option_contracts", "documented_contracts.json")[name])


class _ContractClient:
    """The client seam: answers a contract lookup with the payload the test built, or ``None`` for a 404."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.asked: list[str] = []

    async def get_option_contract(self, symbol: str) -> object:
        self.asked.append(symbol)
        return self.payload


def test_the_documented_listing_maps_every_field_and_is_one_a_bot_may_trade() -> None:
    listing = from_alpaca_option_contract(_example(), observed_at_ms=_OBSERVED)

    assert listing.broker == "alpaca", "the documented listing maps broker"
    assert listing.contract_id == "98359ef7-5124-49f3-85ea-5cf02df6defa", "the documented listing maps contract_id"
    assert listing.symbol == "AAPL250620C00100000", "the documented listing maps symbol"
    assert listing.name == "AAPL Jun 20 2025 100 Call", "the documented listing maps name"
    assert listing.status == "active", "the documented listing maps status"
    assert listing.tradable is True, "the documented listing maps tradable"
    # A date becomes its 16:00 New York anchor, once, here.
    assert listing.expiry_ms == option_expiry_anchor_ms(date(2025, 6, 20)), "the documented listing maps expiry_ms"
    assert listing.root_symbol == "AAPL", "the documented listing maps root_symbol"
    assert listing.underlying_symbol == "AAPL", "the documented listing maps underlying_symbol"
    assert listing.underlying_asset_id == "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", (
        "the documented listing maps underlying_asset_id"
    )
    assert listing.right == "call", "the documented listing maps right"
    assert listing.style == "american", "the documented listing maps style"
    assert listing.strike_price == Decimal("100"), "the documented listing maps strike_price"
    assert listing.multiplier == Decimal("100"), "the documented listing maps multiplier"
    assert listing.shares_per_contract == Decimal("100"), "the documented listing maps shares_per_contract"
    assert listing.penny_program is True, "the documented listing maps penny_program"
    assert listing.deliverables is None, "the documented listing maps deliverables"
    assert listing.observed_at_ms == _OBSERVED, "the documented listing maps observed_at_ms"
    assert option_contract_refusal(_AAPL_JUN_100_CALL, listing) is None, (
        "the documented contract is one a bot may trade"
    )


def test_a_strike_with_cents_is_read_exactly() -> None:
    payload = _example()
    payload.update(symbol="AAPL250620C00452500", strike_price="452.5")

    listing = from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED)

    assert listing.strike_price == Decimal("452.5")
    assert option_contract_refusal(OptionContract.from_occ_symbol("AAPL250620C00452500"), listing) is None


def test_a_listing_without_its_root_is_still_read() -> None:
    """Alpaca's reference does not list ``root_symbol`` among the fields it requires."""
    payload = _example()
    del payload["root_symbol"]

    listing = from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED)

    assert listing.root_symbol is None
    assert option_contract_refusal(_AAPL_JUN_100_CALL, listing) is None


def test_a_share_class_underlying_keeps_both_spellings() -> None:
    """How Alpaca answers for such a contract is not documented; this holds only that neither field is rewritten."""
    payload = _example()
    payload.update(symbol="BRKB250620C00450000", root_symbol="BRKB", underlying_symbol="BRK.B", strike_price="450")

    listing = from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED)

    assert (listing.root_symbol, listing.underlying_symbol) == ("BRKB", "BRK.B")


def _standard_deliverable() -> dict[str, Any]:
    """One deliverable, assembled from the reference's per-field examples; no page shows a whole one."""
    return {
        "type": "equity",
        "symbol": "AAPL",
        "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "amount": "100",
        "allocation_percentage": "100",
        "settlement_type": "T+2",
        "settlement_method": "CCC",
        "delayed_settlement": False,
    }


def test_deliverables_are_read_when_the_answer_carries_them() -> None:
    payload = _example()
    payload["deliverables"] = [_standard_deliverable()]

    listing = from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED)

    assert listing.deliverables is not None
    (deliverable,) = listing.deliverables
    assert (deliverable.kind, deliverable.symbol, deliverable.amount) == ("equity", "AAPL", Decimal("100"))
    assert option_contract_refusal(_AAPL_JUN_100_CALL, listing) is None


def test_an_adjusted_contract_that_also_delivers_cash_is_refused_as_not_standard() -> None:
    payload = _example()
    payload["deliverables"] = [
        _standard_deliverable(),
        {**_standard_deliverable(), "type": "cash", "symbol": "USD", "amount": "12.50"},
    ]

    refusal = option_contract_refusal(_AAPL_JUN_100_CALL, from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED))

    assert refusal is not None
    assert refusal.reason is OptionContractRefusal.NOT_STANDARD
    assert "AAPL250620C00100000" in str(refusal)


def test_a_deliverable_whose_amount_is_not_settled_is_read_as_unknown_and_refused() -> None:
    payload = _example()
    payload["deliverables"] = [{**_standard_deliverable(), "amount": None}]

    listing = from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED)

    assert listing.deliverables is not None and listing.deliverables[0].amount is None
    refusal = option_contract_refusal(_AAPL_JUN_100_CALL, listing)
    assert refusal is not None and refusal.reason is OptionContractRefusal.NOT_STANDARD


def test_the_older_pages_example_states_no_multiplier_and_is_not_read_as_a_standard_contract() -> None:
    """Alpaca's Options Trading page shows a contract without ``multiplier``; its reference requires one."""
    with pytest.raises(TypeError, match="multiplier"):
        from_alpaca_option_contract(_example("options_trading_page_example"), observed_at_ms=_OBSERVED)


@pytest.mark.parametrize(
    "change",
    [
        {"tradable": "true"},
        {"tradable": None},
        {"status": ""},
        {"type": "straddle"},
        {"style": None},
        {"expiration_date": "next Friday"},
        {"expiration_date": None},
        {"strike_price": "one hundred"},
        {"strike_price": None},
        {"size": None},
        {"multiplier": True},
        {"underlying_symbol": ""},
        {"symbol": None},
        {"id": None},
        {"ppind": "yes"},
        {"deliverables": "none"},
        {"deliverables": ["AAPL"]},
    ],
)
def test_a_listing_missing_what_decides_tradability_is_refused_not_guessed(change: dict[str, Any]) -> None:
    payload = _example()
    payload.update(change)

    with pytest.raises((TypeError, ValueError)):
        from_alpaca_option_contract(payload, observed_at_ms=_OBSERVED)


async def test_the_broker_asks_alpaca_by_the_contracts_own_symbol() -> None:
    client = _ContractClient(_example())

    listing = await AlpacaBroker(client=client).get_option_contract(_AAPL_JUN_100_CALL)  # type: ignore[arg-type]

    assert client.asked == ["AAPL250620C00100000"]
    assert listing is not None and listing.symbol == "AAPL250620C00100000"


async def test_a_contract_alpaca_does_not_list_is_refused_by_name() -> None:
    """IBKR lists it, Alpaca answers 404: the bot is told which contract, and that it is not listed."""
    unlisted = OptionContract.from_occ_symbol("TSLA231110C00021000")

    listing = await AlpacaBroker(client=_ContractClient(None)).get_option_contract(unlisted)  # type: ignore[arg-type]
    refusal = option_contract_refusal(unlisted, listing)

    assert listing is None
    assert refusal is not None
    assert (refusal.reason, refusal.symbol) == (OptionContractRefusal.NOT_LISTED, "TSLA231110C00021000")


async def test_an_answer_about_another_contract_is_refused() -> None:
    asked = OptionContract.from_occ_symbol("AAPL250620C00105000")

    listing = await AlpacaBroker(client=_ContractClient(_example())).get_option_contract(asked)  # type: ignore[arg-type]
    refusal = option_contract_refusal(asked, listing)

    assert refusal is not None
    assert refusal.reason is OptionContractRefusal.NOT_THE_CONTRACT_ASKED_FOR


@pytest.mark.parametrize("payload", [{"symbol": "AAPL250620C00100000"}, [], "AAPL250620C00100000"])
async def test_the_broker_names_a_listing_it_cannot_read_as_unavailable_evidence(payload: object) -> None:
    with pytest.raises(BrokerEvidenceUnavailable, match="option contract"):
        await AlpacaBroker(client=_ContractClient(payload)).get_option_contract(_AAPL_JUN_100_CALL)  # type: ignore[arg-type]

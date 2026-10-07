"""Who takes an option order: Alpaca does, a Dry Run's simulation takes one of a single leg, a shadow world none.

A caller that gates on capabilities is told so, and one that asks for what
is not taken gets a refusal that can never be mistaken for an order of
unknown outcome. How the simulation fills the single-leg order it does take
is ``test_synthetic_broker_option_orders.py``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES, ALPACA_PAPER_CAPABILITIES
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.synthetic_broker import SYNTHETIC_CAPABILITIES, SyntheticBroker
from app.broker.contract.errors import BrokerOptionsNotSupported, BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract

_REF = "learn-ai/spy-options/v1:enter-1"
_CALL = OptionContract.from_occ_symbol("SPY261218C00450000")
_HIGHER_CALL = OptionContract.from_occ_symbol("SPY261218C00455000")

_LONG_CALL = SingleOptionOrderRequest(
    contract=_CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, quantity=1, order_type=OrderType.MARKET
)
_CALL_SPREAD = MultiLegOptionOrderRequest(
    legs=(
        OptionLegRequest(contract=_CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, ratio=1),
        OptionLegRequest(contract=_HIGHER_CALL, side=OrderSide.SELL, intent=OptionPositionIntent.OPEN, ratio=1),
    ),
    quantity=1,
    order_type=OrderType.LIMIT,
    net_limit_price=Decimal("1.94"),
)


class _LiveRead:
    """The live Alpaca read port a shadow world reads account truth through."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.contracts_asked: list[OptionContract] = []
        self.option_account_reads = 0

    def capabilities(self) -> Any:
        return ALPACA_LIVE_CAPABILITIES

    async def get_option_contract(self, contract: OptionContract) -> str:
        self.contracts_asked.append(contract)
        return "alpaca's listing"

    async def get_option_account(self) -> str:
        self.option_account_reads += 1
        return "alpaca's option account"


def test_alpaca_says_it_takes_option_orders_of_one_leg_and_of_several() -> None:
    for capabilities in (ALPACA_PAPER_CAPABILITIES, ALPACA_LIVE_CAPABILITIES):
        assert capabilities.supports_option_orders is True
        assert capabilities.supports_multi_leg_option_orders is True


def test_the_simulation_says_it_takes_an_option_order_of_one_leg_and_none_of_several() -> None:
    assert SYNTHETIC_CAPABILITIES.supports_option_orders is True
    assert SYNTHETIC_CAPABILITIES.supports_multi_leg_option_orders is False
    assert SyntheticBroker(account_id="sim:options-1").capabilities() is SYNTHETIC_CAPABILITIES


def test_a_shadow_world_says_it_takes_no_option_orders_though_its_live_broker_does(tmp_path: Path) -> None:
    """Its trade port synthesizes stock fills only, so Alpaca's "yes" is not passed through as its own."""
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)

    shadow = ports.read.capabilities()

    assert (shadow.supports_option_orders, shadow.supports_multi_leg_option_orders) == (False, False)
    # Every other fact is still the live broker's own.
    unchanged = {"supports_option_orders", "supports_multi_leg_option_orders"}
    assert shadow.model_dump(exclude=unchanged) == ALPACA_LIVE_CAPABILITIES.model_dump(exclude=unchanged)


@pytest.mark.parametrize("order", [_LONG_CALL, _CALL_SPREAD], ids=["one leg", "several legs"])
async def test_the_simulation_refuses_an_option_order_definitively(order: OptionOrderRequest) -> None:
    """One of several legs it never takes. One of a single leg it cannot take here: this simulation has no
    order ledger to record a fill in."""
    broker = SyntheticBroker(account_id="sim:options-1")

    with pytest.raises(BrokerOptionsNotSupported, match="takes no option orders") as refused:
        await broker.submit_option_order(order, client_order_id=_REF)

    # Nothing was sent, so this is never an outcome a lookup must settle.
    assert not isinstance(refused.value, BrokerUnavailable)
    assert refused.value.broker == "synthetic"
    assert await broker.list_orders() == []


@pytest.mark.parametrize("order", [_LONG_CALL, _CALL_SPREAD], ids=["one leg", "several legs"])
async def test_a_shadow_order_book_refuses_an_option_order_definitively(
    tmp_path: Path, order: OptionOrderRequest
) -> None:
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)

    with pytest.raises(BrokerOptionsNotSupported, match="takes no option orders") as refused:
        await ports.trade.submit_option_order(order, client_order_id=_REF)

    assert not isinstance(refused.value, BrokerUnavailable)
    assert ports.book.orders() == []


async def test_a_shadow_order_book_holds_no_option_order_to_look_up(tmp_path: Path) -> None:
    """ "Absent" would be an answer about an order; a shadow world never held one to answer about."""
    shadow = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path).trade

    with pytest.raises(BrokerOptionsNotSupported, match="holds no option orders"):
        await shadow.get_option_order_by_client_order_id(_REF)
    with pytest.raises(BrokerOptionsNotSupported, match="holds no option orders"):
        await shadow.get_option_order_by_broker_order_id("83f37e9f-6b1f-49ed-8fc6-3e6af716323f")


async def test_the_simulation_answers_that_it_holds_no_such_option_order() -> None:
    """It can hold option orders now, so "none by that id" is an answer it can give."""
    simulation = SyntheticBroker(account_id="sim:options-1")

    assert await simulation.get_option_order_by_client_order_id(_REF) is None
    assert await simulation.get_option_order_by_broker_order_id("83f37e9f-6b1f-49ed-8fc6-3e6af716323f") is None


async def test_the_simulation_has_no_contract_reference_and_does_not_call_a_contract_unlisted() -> None:
    simulation = SyntheticBroker(account_id="sim:options-1")

    with pytest.raises(BrokerOptionsNotSupported, match="no option contract reference"):
        await simulation.get_option_contract(_CALL)


async def test_the_simulated_account_states_no_option_buying_power_and_no_level() -> None:
    options = await SyntheticBroker(account_id="sim:options-1", clock=lambda: 1_700_000_000_000).get_option_account()

    assert options.account_id == "sim:options-1"
    assert options.options_buying_power is None
    assert options.options_approved_level is None
    assert options.options_trading_level is None


async def test_a_shadow_world_reads_the_contract_reference_and_option_account_from_the_live_account(
    tmp_path: Path,
) -> None:
    """A Live Shadow applies the Live bot's refusals, so it asks the same Alpaca the Live bot asks."""
    live = _LiveRead()
    ports = compose_shadow_ports(live_read=live, live_account_id="9LIVE0001", artifacts_root=tmp_path)

    assert await ports.read.get_option_contract(_CALL) == "alpaca's listing"
    assert await ports.read.get_option_account() == "alpaca's option account"
    assert live.contracts_asked == [_CALL]
    assert live.option_account_reads == 1


async def test_the_refusal_reaches_a_caller_through_the_clerks_guarded_ports() -> None:
    simulation = SyntheticBroker(account_id="sim:options-1")
    _read, trade = guard_broker_ports(read=simulation, trade=simulation, intake=ReentrantAsyncLock())

    with pytest.raises(BrokerOptionsNotSupported):
        await trade.submit_option_order(_CALL_SPREAD, client_order_id=_REF)

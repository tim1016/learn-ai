"""An option order, its lookups and the option reads stay inside the Clerk's intake fence.

``test_intake_fence.py`` proves every async port method has a guard method.
This proves what each option guard does: refuses before the broker is
touched while intake is fenced, and passes the call through otherwise.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    BrokerCallUnderIntakeError,
    GuardedBrokerReadPort,
    GuardedBrokerTradePort,
    guard_broker_ports,
)
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionOrderRequest,
    OptionPositionIntent,
)
from app.options.contract import OptionContract

_REF = "learn-ai/spy-options/v1:enter-1"
_PARENT_ID = "83f37e9f-6b1f-49ed-8fc6-3e6af716323f"
_CALL = OptionContract.from_occ_symbol("SPY261218C00450000")
_SPREAD = MultiLegOptionOrderRequest(
    legs=(
        OptionLegRequest(contract=_CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, ratio=1),
        OptionLegRequest(
            contract=OptionContract.from_occ_symbol("SPY261218C00455000"),
            side=OrderSide.SELL,
            intent=OptionPositionIntent.OPEN,
            ratio=1,
        ),
    ),
    quantity=1,
    order_type=OrderType.LIMIT,
    net_limit_price=Decimal("1.94"),
)


class _OptionBroker:
    """A broker that records every option call that reached it."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.reached: list[tuple[Any, ...]] = []

    async def submit_option_order(self, order: OptionOrderRequest, *, client_order_id: str) -> str:
        self.reached.append(("submit_option_order", order, client_order_id))
        return "the order"

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> str:
        self.reached.append(("get_option_order_by_client_order_id", client_order_id))
        return "the order by client id"

    async def get_option_order_by_broker_order_id(self, order_id: str) -> str:
        self.reached.append(("get_option_order_by_broker_order_id", order_id))
        return "the order by broker id"

    async def get_option_contract(self, contract: OptionContract) -> str:
        self.reached.append(("get_option_contract", contract))
        return "the listing"

    async def get_option_account(self) -> str:
        self.reached.append(("get_option_account",))
        return "the option account"


_Call = Callable[[GuardedBrokerReadPort, GuardedBrokerTradePort], Awaitable[Any]]

# Each option call: how it is made, what reaches the broker, and what comes back.
_CALLS: dict[str, tuple[_Call, tuple[Any, ...], str]] = {
    "submit_option_order": (
        lambda _read, trade: trade.submit_option_order(_SPREAD, client_order_id=_REF),
        ("submit_option_order", _SPREAD, _REF),
        "the order",
    ),
    "get_option_order_by_client_order_id": (
        lambda _read, trade: trade.get_option_order_by_client_order_id(_REF),
        ("get_option_order_by_client_order_id", _REF),
        "the order by client id",
    ),
    "get_option_order_by_broker_order_id": (
        lambda _read, trade: trade.get_option_order_by_broker_order_id(_PARENT_ID),
        ("get_option_order_by_broker_order_id", _PARENT_ID),
        "the order by broker id",
    ),
    "get_option_contract": (
        lambda read, _trade: read.get_option_contract(_CALL),
        ("get_option_contract", _CALL),
        "the listing",
    ),
    "get_option_account": (
        lambda read, _trade: read.get_option_account(),
        ("get_option_account",),
        "the option account",
    ),
}


@pytest.mark.parametrize("method", sorted(_CALLS))
async def test_an_option_call_made_while_intake_is_fenced_never_reaches_the_broker(method: str) -> None:
    fence = ReentrantAsyncLock()
    broker = _OptionBroker()
    read, trade = guard_broker_ports(read=broker, trade=broker, intake=fence)
    call, _reaches, _answer = _CALLS[method]

    async with fence:
        with pytest.raises(BrokerCallUnderIntakeError, match=method):
            await call(read, trade)

    assert broker.reached == []


@pytest.mark.parametrize("method", sorted(_CALLS))
async def test_an_option_call_made_outside_the_fence_reaches_the_broker_unchanged(method: str) -> None:
    broker = _OptionBroker()
    read, trade = guard_broker_ports(read=broker, trade=broker, intake=ReentrantAsyncLock())
    call, reaches, answer = _CALLS[method]

    assert await call(read, trade) == answer
    assert broker.reached == [reaches]

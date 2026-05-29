"""Tests for NoSubmitBrokerAdapter (PRD-C).

Asserts on observable boundary behaviour: ib.placeOrder is NEVER called,
place_order returns a shadow ack, advance_bar synthesises shadow_sim fills
via the simulator, and the shadow namespace invariant is enforced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.data.trade_bar import TradeBar
from app.engine.live.no_submit_broker_adapter import (
    NoSubmitBrokerAdapter,
    ShadowInvariantBreached,
)


def _spec(action: str = "BUY", qty: float = 1.0) -> IbkrOrderSpec:
    return IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action=action,
        quantity=qty,
        order_type="MKT",
        time_in_force="DAY",
        confirm_paper=True,
    )


def _bar(minute: int, open_: str = "500") -> TradeBar:
    start = datetime(2026, 5, 4, 14, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(open_),
        low=Decimal(open_),
        close=Decimal(open_),
        volume=100,
    )


def _adapter(ib=None) -> NoSubmitBrokerAdapter:
    return NoSubmitBrokerAdapter(
        ib or SimpleNamespace(),
        strategy_instance_id="spy_vwap_reversion_1min",
        bot_order_namespace="learn-ai/spy_vwap_reversion_1min/abc",
    )


@pytest.mark.asyncio
async def test_place_order_never_calls_ib_place_order() -> None:
    ib = SimpleNamespace(placeOrder=MagicMock())
    adapter = _adapter(ib)

    await adapter.place_order(_spec())

    ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_returns_shadow_ack() -> None:
    from app.engine.live.no_submit_broker_adapter import ShadowOrderAck

    adapter = _adapter()
    ack = await adapter.place_order(_spec())
    assert isinstance(ack, ShadowOrderAck)
    assert ack.submit_mode == "shadow"
    assert ack.is_paper is True


@pytest.mark.asyncio
async def test_advance_bar_synthesises_shadow_sim_fill_at_next_open() -> None:
    adapter = _adapter()
    # Bar the strategy acts on, then place an order, then the next bar arrives.
    await adapter.advance_bar(_bar(30, "500"))  # current bar = :30
    await adapter.place_order(_spec(action="BUY", qty=1))
    await adapter.advance_bar(_bar(31, "501"))  # next bar opens at 501 → fill

    events = adapter.drain_order_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.execution_source == "shadow_sim"
    assert ev.fill_price == Decimal("501")
    assert ev.fill_quantity == 1
    assert ev.source_bar_close_ms == int(datetime(2026, 5, 4, 14, 31, tzinfo=UTC).timestamp() * 1000)


@pytest.mark.asyncio
async def test_cancel_open_orders_is_noop() -> None:
    adapter = _adapter()
    assert await adapter.cancel_open_orders() == []


def test_shadow_invariant_raises_on_broker_open_orders() -> None:
    adapter = _adapter()
    with pytest.raises(ShadowInvariantBreached) as exc:
        adapter.assert_shadow_invariant(open_orders=[object()], positions=[])
    assert exc.value.reason == "unexpected_open_order_at_broker"


def test_shadow_invariant_passes_on_empty_namespace() -> None:
    adapter = _adapter()
    adapter.assert_shadow_invariant(open_orders=[], positions=[])  # no raise

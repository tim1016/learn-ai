"""Unit tests for ``_recovery_flatten`` in ``app.engine.live.run``.

The cmd_start integration (exception → recovery flatten → exit 3) is
covered end-to-end in the FakeBroker shutdown integration test; these
tests verify the recovery helper in isolation.
"""

from __future__ import annotations

import pytest

from app.broker.ibkr.models import (
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.run import _recovery_flatten, _resolve_recovery_broker
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _seed_position(broker: FakeBroker, symbol: str, quantity: float) -> None:
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol=symbol,
                sec_type="STK",
                quantity=quantity,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )


@pytest.mark.asyncio
async def test_recovery_flatten_submits_one_sell_per_long_position() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "SPY"
    assert sell_orders[0].quantity == 100


@pytest.mark.asyncio
async def test_recovery_flatten_submits_one_buy_per_short_position() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", -50.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    buy_orders = [o for o in broker.orders if o.action == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0].symbol == "SPY"
    assert buy_orders[0].quantity == 50


@pytest.mark.asyncio
async def test_recovery_flatten_no_positions_returns_zero() -> None:
    broker = FakeBroker()
    liquidated = await _recovery_flatten(broker)
    assert liquidated == 0
    assert broker.orders == []


@pytest.mark.asyncio
async def test_recovery_flatten_cancel_failure_does_not_block_flatten() -> None:
    """A broker failure on cancel_open_orders must not skip the flatten path.

    The whole point of recovery flatten is to leave the account flat
    even when something has misbehaved. Swallowing the cancel failure
    and proceeding to liquidate matches that intent.
    """

    class RaisingCancelBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise RuntimeError("simulated broker timeout on cancel")

    broker = RaisingCancelBroker()
    _seed_position(broker, "SPY", 50.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1


@pytest.mark.asyncio
async def test_recovery_flatten_per_position_place_order_failure_keeps_loop() -> None:
    """If place_order fails for symbol A, symbol B still gets an attempt."""

    class FailFirstThenSucceedBroker(FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def place_order(self, spec):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("simulated place_order failure on first call")
            return await super().place_order(spec)

    broker = FailFirstThenSucceedBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=1,
                symbol="SPY",
                sec_type="STK",
                quantity=100.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
            IbkrPosition(
                account_id="DU123",
                con_id=2,
                symbol="QQQ",
                sec_type="STK",
                quantity=50.0,
                avg_cost=400.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    liquidated = await _recovery_flatten(broker)

    # First position's place_order raised; second succeeded.
    assert liquidated == 1


def test_resolve_recovery_broker_prefers_injected_broker() -> None:
    fake = FakeBroker()
    resolved = _resolve_recovery_broker(fake, None)
    assert resolved is fake


def test_resolve_recovery_broker_returns_none_when_no_broker_no_client() -> None:
    assert _resolve_recovery_broker(None, None) is None


def test_resolve_recovery_broker_returns_none_when_client_disconnected() -> None:
    class _DisconnectedClient:
        def is_connected(self) -> bool:
            return False

    assert _resolve_recovery_broker(None, _DisconnectedClient()) is None

"""Phase 5C / VCR-0002 — cancel-confirm timeout in ``_recovery_flatten``.

The recovery-flatten path is the engine's exception-handling liquidation
helper called from ``cmd_start`` on fatal failure. Per PRD §5C it MUST
NOT liquidate when the broker's cancel-confirm doesn't return within
``CANCEL_CONFIRM_TIMEOUT_S`` — racing the cancel against the immediately
following market liquidation is exactly the safety hole §5C closes.

The behavior on timeout:

* ``CancelConfirmTimeoutHaltError`` raises out of ``_recovery_flatten``
* Caller (cmd_start) is responsible for surfacing the halt to the
  runner exit code; recovery_flatten itself never silently proceeds.

The fast path (broker returns within the timeout) is unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from app.broker.ibkr.models import IbkrPosition, IbkrPositionsSnapshot
from app.engine.live.live_engine import (
    CANCEL_CONFIRM_TIMEOUT_S,
    CancelConfirmTimeoutHaltError,
)
from app.engine.live.run import _recovery_flatten
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


class _HangingCancelBroker(FakeBroker):
    """Broker whose ``cancel_open_orders`` never returns — models a
    broker session that has lost the confirm callback path. Recovery
    flatten must refuse to liquidate rather than silently proceed."""

    async def cancel_open_orders(self) -> list[int]:
        await asyncio.sleep(CANCEL_CONFIRM_TIMEOUT_S * 10)
        return []


@pytest.mark.asyncio
async def test_recovery_flatten_raises_on_cancel_confirm_timeout(monkeypatch) -> None:
    broker = _HangingCancelBroker()
    _seed_position(broker, "SPY", 100.0)
    # Shorten the timeout so the test runs sub-second.
    monkeypatch.setattr(
        "app.engine.live.live_engine.CANCEL_CONFIRM_TIMEOUT_S", 0.05
    )

    with pytest.raises(CancelConfirmTimeoutHaltError) as exc_info:
        await _recovery_flatten(broker)

    assert exc_info.value.timeout_s == pytest.approx(0.05)
    # Critical: NO place_order call after the timeout — the liquidation
    # leg must not run.
    assert broker.orders == []


@pytest.mark.asyncio
async def test_recovery_flatten_fast_cancel_path_liquidates() -> None:
    """Sanity: a broker whose ``cancel_open_orders`` returns promptly
    still liquidates as before — the timeout wrapper is transparent on
    the happy path."""
    broker = FakeBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "SPY"


@pytest.mark.asyncio
async def test_recovery_flatten_non_timeout_exception_tolerated() -> None:
    """The pre-existing tolerance for non-timeout exceptions (logged,
    continues to liquidation) is preserved — only ``TimeoutError`` is
    upgraded to a halt. Models a transient broker glitch on cancel that
    the recovery-flatten path historically logged-and-continued."""

    class _RaisingCancelBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise RuntimeError("transient broker glitch")

    broker = _RaisingCancelBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    assert len([o for o in broker.orders if o.action == "SELL"]) == 1

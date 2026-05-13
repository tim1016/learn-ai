"""Tests for LiveEngine driver behavior before the full replay gate."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.broker.ibkr.models import IbkrPosition, IbkrPositionsSnapshot
from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


def _bar(minute: int, open_: str, close: str) -> TradeBar:
    return _bar_at(14, minute, open_, close)


def _bar_at(hour: int, minute: int, open_: str, close: str) -> TradeBar:
    start = datetime(2026, 5, 4, hour, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=max(Decimal(open_), Decimal(close)),
        low=min(Decimal(open_), Decimal(close)),
        close=Decimal(close),
        volume=100,
    )


class OneEntryStrategy(Strategy):
    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        if not self.ctx.portfolio.pending_orders and not self.ctx.portfolio.get_position("SPY").quantity:
            self.ctx.set_holdings("SPY", Decimal("1"))


@pytest.mark.asyncio
async def test_live_engine_processes_bar_signal_submission_and_next_bar_fill() -> None:
    broker = FakeBroker()
    engine = LiveEngine(None, LiveConfig(), broker=broker)
    bars = [_bar(minute, "500", "500") for minute in range(30, 47)]

    result = await engine.run(OneEntryStrategy(), iter_bars(bars))

    assert result.submitted_order_ids == [1]
    assert len(result.order_events) == 1
    assert result.order_events[0].time == bars[-1].time
    assert result.order_events[0].fill_price == Decimal("500")
    assert result.order_events[0].fill_quantity == 200
    assert result.pending_orders == 0
    assert result.open_positions == {"SPY": 200}
    assert len(result.equity_curve) == len(bars)


class HoldsExistingStrategy(Strategy):
    """Inherits an open position from the broker; emits no signals.

    Used to exercise the force-flat barrier in isolation: the position
    exists at run start, the strategy never submits an order on its own,
    and ``on_force_flat`` flips a flag the test reads.
    """

    def __init__(self) -> None:
        super().__init__()
        self.force_flat_called: bool = False

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        self.ctx.add_equity("SPY")
        # 15-min consolidator satisfies the single-symbol guard; the
        # bar window in the test is too short to fire it.
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self._noop)

    def _noop(self, _bar: TradeBar) -> None:
        return None

    def on_force_flat(self) -> None:
        self.force_flat_called = True


@pytest.mark.asyncio
async def test_live_engine_force_flat_liquidates_open_positions_at_threshold() -> None:
    broker = FakeBroker()
    # Pre-seed an open SPY position. ``LivePortfolio.refresh_from_broker``
    # picks this up before the bar loop starts.
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=100.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    engine = LiveEngine(None, LiveConfig(), broker=broker)
    strategy = HoldsExistingStrategy()
    # Bars span 15:53 → 15:58. force_flat_at default is 15:55, so the
    # barrier fires at the 15:55 bar; the liquidation fills under
    # FakeBroker on the next bar's open (15:56).
    bars = [_bar_at(15, m, "500", "500") for m in range(53, 59)]

    result = await engine.run(strategy, iter_bars(bars))

    # Force-flat ran exactly once.
    assert strategy.force_flat_called is True

    # One liquidation order was submitted via the broker boundary.
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "SPY"
    assert sell_orders[0].quantity == 100

    # The liquidation filled (FakeBroker.advance_bar processed it on the
    # next bar after submission). ``fill_quantity`` is signed.
    liquidation_fills = [e for e in result.order_events if e.fill_quantity == -100]
    assert len(liquidation_fills) == 1
    assert liquidation_fills[0].symbol == "SPY"

    # Final state: position is flat and nothing is pending.
    assert result.open_positions == {}
    assert result.pending_orders == 0


class IdleStrategy(Strategy):
    """No-op strategy used to verify force-flat does not fire prematurely."""

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(
            "SPY",
            timedelta(minutes=15),
            lambda _bar: None,
        )


@pytest.mark.asyncio
async def test_live_engine_force_flat_does_not_fire_before_threshold() -> None:
    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=50.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    engine = LiveEngine(None, LiveConfig(), broker=broker)
    strategy = IdleStrategy()
    # All bars are well before the 15:55 default — force-flat must not fire.
    bars = [_bar(m, "500", "500") for m in range(30, 35)]

    result = await engine.run(strategy, iter_bars(bars))

    assert broker.orders == []
    assert result.order_events == []
    assert result.open_positions == {"SPY": 50}


@pytest.mark.asyncio
async def test_live_engine_shutdown_event_breaks_loop_and_flattens_open_position() -> None:
    """SIGINT/SIGTERM graceful shutdown: cancel + flatten + submit, then exit clean.

    Pre-seeds an open SPY position via the broker snapshot, sets
    shutdown_event before run() starts, and asserts the engine
    flattens the position on the first bar's top-of-iteration check.
    The liquidation order is submitted to the broker; the actual fill
    happens broker-side after run() returns (FakeBroker.advance_bar
    is not reached because we break before processing the bar), which
    mirrors real IBKR: the operator's goal is broker-side flat, not
    portfolio-cache flat.
    """
    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=100.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    engine = LiveEngine(None, LiveConfig(), broker=broker)
    strategy = IdleStrategy()
    bars = [_bar(m, "500", "500") for m in range(30, 35)]

    shutdown_event = asyncio.Event()
    shutdown_event.set()  # Pre-set: first iteration's check trips immediately.

    result = await engine.run(strategy, iter_bars(bars), shutdown_event=shutdown_event)

    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "SPY"
    assert sell_orders[0].quantity == 100
    assert len(result.submitted_order_ids) == 1


@pytest.mark.asyncio
async def test_live_engine_shutdown_event_with_no_positions_exits_clean() -> None:
    """Shutdown with empty portfolio: no flatten orders, engine returns clean."""
    broker = FakeBroker()  # No position snapshot — portfolio loads empty.
    engine = LiveEngine(None, LiveConfig(), broker=broker)
    strategy = IdleStrategy()
    bars = [_bar(m, "500", "500") for m in range(30, 35)]

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    result = await engine.run(strategy, iter_bars(bars), shutdown_event=shutdown_event)

    # No positions → nothing to liquidate, no orders submitted.
    assert broker.orders == []
    assert result.submitted_order_ids == []
    assert result.open_positions == {}


@pytest.mark.asyncio
async def test_live_engine_shutdown_event_unset_runs_normally() -> None:
    """Default shutdown_event=None preserves the prior loop behavior exactly."""
    broker = FakeBroker()
    engine = LiveEngine(None, LiveConfig(), broker=broker)
    bars = [_bar(minute, "500", "500") for minute in range(30, 47)]

    result = await engine.run(OneEntryStrategy(), iter_bars(bars))

    assert result.submitted_order_ids == [1]
    assert len(result.order_events) == 1
    assert result.open_positions == {"SPY": 200}


@pytest.mark.asyncio
async def test_live_engine_emits_per_bar_heartbeat_log(caplog) -> None:
    """Engine emits a `[BAR]` heartbeat log per minute_bar received.

    Operability requirement (issue #228): operators tail live.log to
    distinguish "engine running, strategy in warmup" from "engine hung."
    Without a per-bar log line the warmup window is silent and looks
    indistinguishable from a hang — issue #227 was that exact
    misdiagnosis. The heartbeat must include the bar time, the count
    of consolidator emissions on this bar, and whether the strategy
    published a new decision snapshot.
    """
    caplog.set_level("INFO", logger="app.engine.live.live_engine")

    broker = FakeBroker()
    engine = LiveEngine(None, LiveConfig(), broker=broker)
    bars = [_bar(minute, "500", "500") for minute in range(30, 33)]

    await engine.run(HoldsExistingStrategy(), iter_bars(bars))

    bar_logs = [r for r in caplog.records if r.getMessage().startswith("[BAR]")]
    assert len(bar_logs) == 3, (
        f"expected 3 [BAR] heartbeats (one per minute_bar), got {len(bar_logs)}: {[r.getMessage() for r in bar_logs]}"
    )
    # Each heartbeat must surface the structural diagnostic fields the
    # operator needs to read state at a glance.
    for record in bar_logs:
        msg = record.getMessage()
        assert "consolidator_emitted=" in msg, msg
        assert "snapshot=" in msg, msg

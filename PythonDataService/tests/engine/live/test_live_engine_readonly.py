"""Tests for LiveEngine readonly mode + max-orders-per-day enforcement.

Covers Phase C-2b-iii's two operational-safety features added to
``LiveEngine``:

  - ``readonly=True``: drains the strategy's pending orders without
    calling broker.place_order. Powers Phase D's dry run.
  - ``max_orders_per_day``: halts mid-run when the per-session
    submission count exceeds the cap (§ 9). Counter resets on each
    new session date.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine, MaxOrdersPerDayExceeded
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


def _bar(minute: int, close: str) -> TradeBar:
    """1-minute bar at 14:00 UTC + minute (handles hour rollover)."""
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
    )


class _AlwaysSubmittingStrategy(Strategy):
    """Submits a trivial order on every bar so we can count submissions."""

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        # Submit a fresh entry every consolidated bar; the engine's
        # max-orders-per-day check is what stops us from running away.
        if not self.ctx.portfolio.pending_orders and not self.ctx.portfolio.get_position("SPY").quantity:
            self.ctx.set_holdings("SPY", Decimal("1"))


# ──────────────────────────── readonly mode ──────────────────────────


@pytest.mark.asyncio
async def test_readonly_drains_pending_without_calling_broker(tmp_path: Path) -> None:
    """In readonly mode, the strategy's pending orders are cleared each
    bar but broker.place_order is never invoked — so broker.orders
    stays empty even though the strategy queued orders."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        readonly=True,
    )
    bars = [_bar(minute, "500") for minute in range(30, 60)]

    result = await engine.run(_AlwaysSubmittingStrategy(), iter_bars(bars))

    assert broker.orders == [], "no broker.place_order calls in readonly mode"
    assert result.submitted_order_ids == [], "no order acks recorded"


@pytest.mark.asyncio
async def test_readonly_still_writes_decisions(tmp_path: Path) -> None:
    """Decisions parquet still populates in readonly — that's the dry-run deliverable."""

    class _SnapStrategy(Strategy):
        def initialize(self) -> None:
            from app.engine.strategy.base import DecisionSnapshot

            self._snap_cls = DecisionSnapshot
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            self.last_decision_snapshot = self._snap_cls(
                bar_close_ms=int(bar.end_time.timestamp() * 1000),
                ema5=float(bar.close),
                ema10=float(bar.close),
                rsi=60.0,
                signal="HOLD",
                intended_price=float(bar.close),
            )

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        readonly=True,
    )
    bars = [_bar(minute, "500") for minute in range(30, 80)]
    await engine.run(_SnapStrategy(), iter_bars(bars))

    decisions_path = tmp_path / "decisions.parquet"
    assert decisions_path.exists()
    df = pd.read_parquet(decisions_path)
    assert len(df) >= 1
    # Executions parquet must NOT exist — readonly produces no fills.
    assert not (tmp_path / "executions.parquet").exists()


# ──────────────────────────── max-orders cap ─────────────────────────


@pytest.mark.asyncio
async def test_max_orders_per_day_halts_when_cap_exceeded() -> None:
    """The strategy queues a submission per consolidated bar; with the
    cap at 1 the second bar's submission must trigger
    ``MaxOrdersPerDayExceeded``."""

    class _CapTestStrategy(Strategy):
        """Submit one order per consolidated bar regardless of position
        state — fakes a misbehaving runaway strategy."""

        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            assert self.ctx is not None
            self.ctx.portfolio.submit_market_order("SPY", 1, self.ctx.current_time, tag="cap-test")

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        max_orders_per_day=1,
    )
    bars = [_bar(minute, "500") for minute in range(30, 80)]
    with pytest.raises(MaxOrdersPerDayExceeded):
        await engine.run(_CapTestStrategy(), iter_bars(bars))


@pytest.mark.asyncio
async def test_max_orders_per_day_resets_on_new_session_date() -> None:
    """Counter resets on date change. Cap=2; we span two trading days
    with 1 order each (under cap), so the run completes cleanly."""

    class _OneOrderPerSessionStrategy(Strategy):
        def __init__(self) -> None:
            super().__init__()
            self._submitted_today: dict = {}

        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            assert self.ctx is not None
            day = bar.end_time.date()
            if not self._submitted_today.get(day):
                self.ctx.portfolio.submit_market_order("SPY", 1, self.ctx.current_time, tag="daily")
                self._submitted_today[day] = True

    # Span two trading days. Day boundary at midnight UTC = bar at minute
    # offset N where 14:00 UTC + N min crosses midnight UTC. 14:00 +
    # 600 min = 24:00 = 00:00 next day.
    day1_bars = [_bar(minute, "500") for minute in range(30, 100)]
    day2_bars = [_bar(minute, "500") for minute in range(610, 680)]
    bars = day1_bars + day2_bars

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        max_orders_per_day=2,
    )
    result = await engine.run(_OneOrderPerSessionStrategy(), iter_bars(bars))
    # No exception; both days saw one submission each (under the cap).
    assert len(result.submitted_order_ids) == 2


@pytest.mark.asyncio
async def test_max_orders_per_day_none_disables_cap() -> None:
    """``max_orders_per_day=None`` (default) means no cap — the engine
    should never raise MaxOrdersPerDayExceeded."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        max_orders_per_day=None,
    )
    # Use the same runaway-strategy as the cap test above.

    class _CapTestStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

        def on_bar(self, bar: TradeBar) -> None:
            assert self.ctx is not None
            self.ctx.portfolio.submit_market_order("SPY", 1, self.ctx.current_time, tag="x")

    bars = [_bar(minute, "500") for minute in range(30, 100)]
    # No exception expected.
    await engine.run(_CapTestStrategy(), iter_bars(bars))

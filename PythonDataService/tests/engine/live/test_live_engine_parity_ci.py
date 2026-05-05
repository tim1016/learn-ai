"""CI parity gate: BacktestEngine ≡ LiveEngine on a tiny synthetic fixture.

The full parity gate in ``test_live_engine_replay.py`` is run against a
local Polygon-sourced LEAN cache that is gitignored runtime data and
not materialized on CI runners — that test skips there. This file
provides a small deterministic fixture that runs in CI on every PR:
both engines consume the same in-memory bar stream and must produce
identical order events, equity curves, and trade logs.

The strategy is deliberately minimal — one entry, one exit — so the
test is fast and the assertions stay readable. Coverage of indicator-
heavy strategies, force-flat lifecycle, and the lifecycle-collapse
edge case lives in the sibling files.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode, OrderEvent
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars

_BASE_TIME = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


def _bar(minute_offset: int, open_: str, close: str) -> TradeBar:
    start = _BASE_TIME + timedelta(minutes=minute_offset)
    open_d = Decimal(open_)
    close_d = Decimal(close)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=open_d,
        high=max(open_d, close_d),
        low=min(open_d, close_d),
        close=close_d,
        volume=1000,
    )


def _build_bars() -> list[TradeBar]:
    """Eight one-minute bars with a deterministic price drift.

    Long enough to see two 1-minute consolidator emissions (entry on
    first emit, exit on second), short enough that the assertions stay
    readable. The price path is monotonic so the integer share count
    matches across engines without rounding noise.
    """
    prices = ["500", "501", "502", "503", "504", "505", "506", "507"]
    return [_bar(i, p, p) for i, p in enumerate(prices)]


class _InMemoryReader:
    """Minimal iter_bars-compatible source for BacktestEngine."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        for bar in self._bars:
            if bar.symbol != symbol:
                continue
            bar_date = bar.time.date()
            if bar_date < start or bar_date > end:
                continue
            yield bar


class _EnterThenExitStrategy(Strategy):
    """Enter on first consolidator emit, exit on the second, no further activity."""

    def __init__(self) -> None:
        super().__init__()
        self._stage = 0

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        self.set_start_date(2026, 5, 4)
        self.set_end_date(2026, 5, 4)
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self._on_bar)

    def _on_bar(self, _bar: TradeBar) -> None:
        assert self.ctx is not None
        if self._stage == 0:
            self.ctx.set_holdings("SPY", Decimal("1"))
            self._stage = 1
        elif self._stage == 1:
            self.ctx.liquidate("SPY")
            self._stage = 2


def _signature(events: list[OrderEvent]) -> list[tuple]:
    return [
        (
            e.symbol,
            e.fill_quantity,
            e.fill_price,
            e.direction,
            e.fee,
            e.tag,
            int(e.time.astimezone(UTC).timestamp() * 1000),
        )
        for e in events
    ]


@pytest.mark.asyncio
async def test_live_and_backtest_match_on_synthetic_fixture() -> None:
    bars = _build_bars()

    backtest_result = BacktestEngine(
        data_source=_InMemoryReader(bars),
        fill_model=FillModel(mode=FillMode.NEXT_BAR_OPEN),
    ).run(_EnterThenExitStrategy())

    live_result = await LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=FakeBroker(),
    ).run(_EnterThenExitStrategy(), iter_bars(bars))

    # Same order event sequence — exact match, not "close enough".
    assert _signature(live_result.order_events) == _signature(backtest_result.order_events)

    # Same final cash, equity, and fee total.
    assert live_result.initial_cash == backtest_result.initial_cash
    assert live_result.final_equity - backtest_result.final_equity == Decimal("0")
    assert live_result.total_fees - backtest_result.total_fees == Decimal("0")

    # Equity curves agree timestamp-for-timestamp.
    assert len(live_result.equity_curve) == len(backtest_result.equity_curve)
    for live, expected in zip(live_result.equity_curve, backtest_result.equity_curve, strict=True):
        assert int(live.timestamp.astimezone(UTC).timestamp() * 1000) == int(
            expected.timestamp.astimezone(UTC).timestamp() * 1000
        )
        assert live.equity - expected.equity == Decimal("0")
        assert live.cash - expected.cash == Decimal("0")
        assert live.holdings_value - expected.holdings_value == Decimal("0")

    # Both engines closed the round trip.
    assert live_result.open_positions == {}
    assert backtest_result.equity_curve[-1].holdings_value == Decimal("0")

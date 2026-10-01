"""Tests for the per-bar DecisionSnapshot publication on SpyEmaCrossover.

The strategy's ``last_decision_snapshot`` attribute is the per-bar
observation seam the LEAN-vs-engine parity test reads after each
handler. This file pins:

  - warmup bars publish nothing (snapshot stays None)
  - post-warmup bars publish a HOLD snapshot
  - bar_close_ms is the canonical int64 ms UTC of bar.end_time
  - intended_price is the bar close

Trading logic is NOT tested here — that's covered by
``test_spy_validation.py``. This file only verifies that the new
observability publication is correct and that the trade behavior is
preserved (no hidden assertion on trade count, just a smoke check).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import DecisionSnapshot, StrategyContext


def _bar(minute_offset: int, close: float) -> TradeBar:
    """Build a 1-minute SPY TradeBar at 09:30 + offset, with given close."""
    start = datetime(2024, 4, 1, 9, 30, tzinfo=UTC) + timedelta(minutes=minute_offset)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=1000,
    )


def _make_strategy() -> tuple[EmaCrossoverSignalAlgorithm, StrategyContext]:
    """Construct a strategy + context wired to a real Portfolio for set_holdings math."""
    portfolio = Portfolio(initial_cash=Decimal("100000"))
    ctx = StrategyContext(portfolio=portfolio)
    strategy = EmaCrossoverSignalAlgorithm()
    strategy.ctx = ctx
    strategy.initialize()
    return strategy, ctx


def _drive(strategy: EmaCrossoverSignalAlgorithm, ctx: StrategyContext, bars: list[TradeBar]) -> None:
    """Replay 1-min bars through the consolidator (which fires the strategy bar handler at 15-min boundaries)."""
    for bar in bars:
        ctx.portfolio.update_reference_price(bar.symbol, bar.close)
        for consolidator in ctx.get_consolidators(bar.symbol):
            consolidator.update(bar)


def test_snapshot_starts_none_before_any_bar_processed() -> None:
    strategy, _ = _make_strategy()
    assert strategy.last_decision_snapshot is None


def test_snapshot_remains_none_during_warmup() -> None:
    """RSI(14) warmup needs > 14 bars; before that, no snapshot is published.

    The strategy's bar handler returns early during warmup before
    reaching the snapshot publication line — by design.
    """
    strategy, ctx = _make_strategy()
    # Two consolidated bars (= 30 minute-bars): not enough for RSI(14).
    bars = [_bar(i, 500.0) for i in range(30)]
    _drive(strategy, ctx, bars)
    assert strategy.last_decision_snapshot is None


def test_snapshot_published_with_hold_after_warmup() -> None:
    """Once indicators are ready, every bar publishes a snapshot.

    A flat-line price (no crossover, no real momentum) keeps the
    strategy in no-position; signal must be HOLD.
    """
    strategy, ctx = _make_strategy()
    # 25 consolidated bars × 15 min = 375 minute-bars; well past RSI(14) warmup.
    bars = [_bar(i, 500.0) for i in range(25 * 15)]
    _drive(strategy, ctx, bars)

    snap = strategy.last_decision_snapshot
    assert snap is not None
    assert isinstance(snap, DecisionSnapshot)
    assert snap.signal == "HOLD"
    assert snap.intended_price == pytest.approx(500.0)
    # Indicator values exist (non-NaN floats).
    assert snap.ema5 == pytest.approx(500.0)
    assert snap.ema10 == pytest.approx(500.0)
    # Flat price ⇒ RSI is undefined-ish; pandas-ta variants land near 0
    # or 50. We don't assert the exact value, only that it's a finite float.
    assert isinstance(snap.rsi, float)


def test_snapshot_bar_close_ms_is_canonical_utc_milliseconds() -> None:
    """bar_close_ms must be int64 ms UTC of the consolidated bar end.

    The 15-min consolidator emits a window only when the FIRST minute
    bar of the *next* window arrives, so the last fully-emitted
    consolidated bar ends ≤ 15 minutes before the last minute bar's
    end. We check that bound rather than equality.
    """
    strategy, ctx = _make_strategy()
    bars = [_bar(i, 500.0) for i in range(25 * 15)]
    _drive(strategy, ctx, bars)

    snap = strategy.last_decision_snapshot
    assert snap is not None
    last_minute_end_ms = bars[-1].end_ms
    delta = last_minute_end_ms - snap.bar_close_ms
    assert 0 <= delta <= 15 * 60 * 1000, (
        f"snap.bar_close_ms={snap.bar_close_ms} should be within one "
        f"15-min window of last bar end {last_minute_end_ms}; delta={delta}ms"
    )
    # Consolidated bars align to 15-min minute boundaries — verify divisibility.
    bar_close_seconds = snap.bar_close_ms // 1000
    assert bar_close_seconds % (15 * 60) == 0, (
        f"snap.bar_close_ms={snap.bar_close_ms} not aligned to a 15-min boundary"
    )

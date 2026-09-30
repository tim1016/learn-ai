"""End-to-end-lite tests for the engine's end-of-data terminal accounting.

The session-entry cutoff and force-flat barriers these tests once covered
were unused wall-clock literals, deleted under #2607: the session's end is
the canonical calendar's close, and the one rule that reads it lives in
``app.lean_sidecar.closing_bar``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.order import Direction, FillMode
from app.engine.strategy.base import Strategy

_ET = ZoneInfo("America/New_York")


class _StaticBarReader:
    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        yield from self._bars


class _EntryThenExitStrategy(Strategy):
    """Submits a LONG entry on its 1st ``on_bar`` and, optionally, a
    matching exit on the ``exit_on_bar_index``-th callback."""

    def __init__(
        self,
        *,
        exit_on_bar_index: int | None = None,
        exit_quantity: int | None = None,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        skip_entry: bool = False,
    ) -> None:
        super().__init__()
        self._exit_on = exit_on_bar_index
        # None => full exit (-position). An explicit value queues a partial
        # reduction or a flip instead.
        self._exit_quantity = exit_quantity
        self._tp = take_profit
        self._sl = stop_loss
        self._skip_entry = skip_entry
        self._bar_count = 0
        self._symbol = "SPY"
        self.order_events: list = []

    def initialize(self) -> None:
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 1, 3)
        self.set_cash(100_000)
        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol)
        self.ctx.register_consolidator(self._symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        idx = self._bar_count
        self._bar_count += 1
        if idx == 0 and not self._skip_entry:
            self.ctx.portfolio.submit_market_order(
                self._symbol,
                quantity=100,
                submitted_at_ms=bar.end_ms,
                tag="entry",
                take_profit_price=self._tp,
                stop_loss_price=self._sl,
            )
        elif self._exit_on is not None and idx == self._exit_on:
            pos = self.ctx.portfolio.get_position(self._symbol)
            if pos.quantity != 0:
                self.ctx.portfolio.submit_market_order(
                    self._symbol,
                    quantity=self._exit_quantity if self._exit_quantity is not None else -pos.quantity,
                    submitted_at_ms=bar.end_ms,
                    tag="exit",
                )

    def on_order_event(self, event) -> None:
        self.order_events.append(event)


class _EndHookLiquidatingStrategy(_EntryThenExitStrategy):
    """Leaves the position open until the end hook requests liquidation."""

    def on_end_of_algorithm(self) -> None:
        assert self.ctx is not None
        self.ctx.liquidate(self._symbol)


def _bar(hour: int, minute: int, *, high: str = "500", low: str = "500", close: str = "500") -> TradeBar:
    start = datetime(2024, 1, 2, hour, minute, tzinfo=_ET)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


def _run(
    bars: list[TradeBar],
    strategy: Strategy,
    *,
    execution_config: ExecutionConfig | None = None,
    fill_mode: FillMode = FillMode.SIGNAL_BAR_CLOSE,
) -> Strategy:
    config = execution_config or ExecutionConfig(fill_mode=fill_mode)
    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=config,
    )
    engine.run(strategy)
    return strategy


# ===========================================================================
# End-hook liquidation
# ===========================================================================


def test_end_hook_liquidation_is_materialized_as_a_synthetic_terminal_exit():
    """The final close must be a ledger-ready fill, not an open MTM position."""
    bars = [_bar(15, 30), _bar(15, 31), _bar(15, 32, close="505")]
    strategy = _EndHookLiquidatingStrategy()

    _run(bars, strategy)

    assert len(strategy.order_events) == 2
    close_event = strategy.order_events[-1]
    assert close_event.tag == "EndOfAlgorithm"
    assert close_event.direction is Direction.SHORT
    assert close_event.fill_quantity == -100
    assert close_event.fill_price == Decimal("505")
    assert strategy.ctx is not None
    assert strategy.ctx.portfolio.get_position("SPY").quantity == 0


# ===========================================================================
# End-of-data terminal accounting (issue #1928)
# ===========================================================================


def test_end_of_data_closes_a_position_left_by_a_deferred_exit():
    """A NEXT_BAR_OPEN exit submitted on the final bar has no next bar to
    fill against, so it is orphaned in the engine's *local* deferred-fill
    queue. Terminal cleanup drains ``portfolio.pending_orders`` — a
    different queue — so without a holdings-based check the run ends still
    holding the position while reporting no closing trade."""
    bars = [_bar(15, 30), _bar(15, 31), _bar(15, 32), _bar(15, 33)]
    strategy = _EntryThenExitStrategy(exit_on_bar_index=2)

    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=ExecutionConfig(fill_mode=FillMode.NEXT_BAR_OPEN),
    )
    engine.run(strategy)

    assert strategy.ctx is not None
    assert strategy.ctx.portfolio.get_position("SPY").quantity == 0


def test_final_equity_curve_point_matches_final_equity():
    """The last equity snapshot is appended inside the bar loop, before
    terminal liquidation runs. With non-zero costs on the closing trade the
    curve therefore ends above the real final equity, and any consumer that
    compounds curve endpoints across folds propagates the gap."""
    bars = [_bar(15, 30), _bar(15, 31), _bar(15, 32)]
    strategy = _EndHookLiquidatingStrategy()

    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=ExecutionConfig(
            commission_per_order=Decimal("10"),
            slippage_per_share=Decimal("0.10"),
        ),
    )
    result = engine.run(strategy)

    assert result.equity_curve[-1].equity == result.final_equity
    # Restated, not appended. ``exposure_pct`` and ``trading_days`` in
    # app/research/runs/runner.py are derived from the curve's length and its
    # distinct dates, so a duplicate trailing timestamp would corrupt both.
    assert len(result.equity_curve) == len(bars)
    assert result.equity_curve[-1].timestamp_ms == bars[-1].end_ms
    assert len({point.timestamp_ms for point in result.equity_curve}) == len(result.equity_curve)


def test_terminal_close_cost_reaches_the_summarized_statistics():
    """The curve feeds ``results.statistics.summarize``. Restating its final
    point moves drawdown and Sharpe for every run ending in a synthetic exit,
    so pin that the summarized series sees the cost the run actually paid
    rather than the pre-liquidation mark."""
    from app.engine.results.statistics import EquityPoint, summarize

    bars = [_bar(15, 30), _bar(15, 31), _bar(15, 32)]
    strategy = _EndHookLiquidatingStrategy()

    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=ExecutionConfig(
            commission_per_order=Decimal("10"),
            slippage_per_share=Decimal("0.10"),
        ),
    )
    result = engine.run(strategy)

    points = [EquityPoint(timestamp_ms=p.timestamp_ms, equity=p.equity) for p in result.equity_curve]
    stats = summarize(
        initial_cash=result.initial_cash,
        final_equity=result.final_equity,
        trades=[],
        equity_curve=points,
    )
    # Curve is [100000, 99980, 99960]: peak 100000, trough the post-cost close,
    # so drawdown is 40/100000. Before the fix the curve ended at the
    # pre-liquidation mark of 99980 and this same statistic read 0.0002 — a
    # bare ``> 0`` assertion passes either way and pins nothing.
    assert abs(stats["max_drawdown_pct"] - 0.0004) < 1e-9


def test_an_entry_on_the_final_bar_closes_as_a_zero_duration_forced_close():
    """A strategy that enters on the last bar the engine processes is closed
    at that same instant: every sweepable strategy's ``on_end_of_algorithm``
    emits its exit at ``ctx.current_time_ms``, and #1928's terminal sweep
    prices it against the final observed close. Entry and exit therefore share
    a timestamp, which is honest — the round trip really did last no time, and
    there is no later instant to move the exit to. Fabricating one would
    violate ``.claude/rules/temporal-rigor.md``; dropping the trade or
    suppressing the entry would diverge from the strategy's own signal.

    Before the terminal close was admitted, ``validate_trade_log`` rejected the
    pair and ``compute_trade_statistics`` raised ``invalid_trade_times``. That
    reached Strategy Lab and the sync endpoint as an unhandled 500, and failed
    every Grid Search cell and Walk-Forward fold whose window ended on an
    entry."""
    from app.engine.results.statistics import compute_trade_statistics
    from app.engine.strategy.programs.sma_crossover import (
        SmaCrossoverParams,
        build_sma_crossover_signal_program,
    )

    strategy = build_sma_crossover_signal_program(
        SmaCrossoverParams(short_window=2, long_window=3, resolution_minutes=1)
    ).strategy
    # SMA(2) sits below SMA(3) through the fourth bar and crosses above it on
    # the fifth — the last one — so the entry fills at 15:34 and data ends.
    closes = ["500", "499", "498", "497", "505"]
    bars = [_bar(15, 30 + i, high=close, low=close, close=close) for i, close in enumerate(closes)]

    result = BacktestEngine(data_source=_StaticBarReader(bars)).run(strategy)

    assert strategy.ctx is not None
    assert strategy.ctx.portfolio.get_position("SPY").quantity == 0
    assert [event.tag for event in result.order_events] == ["SetHoldings", "EndOfAlgorithm"]
    assert len(strategy.trade_log) == 1
    trade = strategy.trade_log[0]
    assert trade.entry_time_ms == trade.exit_time_ms == bars[-1].end_ms
    # The label is what earns the exemption in ``validate_trade_log``; without
    # it an equal pair stays an error.
    assert trade.is_synthetic_exit is True
    # Zero duration means zero price P&L, but the round trip is still two
    # market orders (SetHoldings entry + EndOfAlgorithm exit) and this run's
    # default ``ExecutionConfig`` charges $1.00 commission per order.
    assert trade.pnl_pts == Decimal(0)
    assert float(result.total_fees) == 2.0
    assert float(result.equity_curve[-1].equity) == float(result.final_equity) == 99998.0
    assert compute_trade_statistics(strategy.trade_log).total_trades == 1


@pytest.mark.parametrize("fill_mode", [FillMode.NEXT_BAR_OPEN, FillMode.NEXT_SESSION_OPEN])
def test_a_deferred_entry_on_the_final_bar_produces_no_trade(fill_mode):
    """Contrast with the zero-duration forced close above: that scenario is
    SIGNAL_BAR_CLOSE, which fills the entry immediately, so the terminal
    sweep has a real position to close. NEXT_BAR_OPEN and NEXT_SESSION_OPEN
    instead defer the fill to the bar *after* the signal bar — a bar that
    never arrives when the signal lands on the engine's last bar. The order
    is orphaned before it ever opens a position, so the run reports no trade
    and no fill at all, mirroring the orphan-cancellation behavior already
    covered for deferred exits and force-flat."""
    bars = [_bar(15, 30)]
    strategy = _EntryThenExitStrategy()

    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=ExecutionConfig(fill_mode=fill_mode),
    )
    engine.run(strategy)

    assert strategy.ctx is not None
    assert strategy.ctx.portfolio.get_position("SPY").quantity == 0
    assert strategy.order_events == []


def test_end_of_data_leaves_a_stranded_partial_reduction_unfilled():
    """Only a *true liquidation* may be synthesized at end of data.

    A deferred order that reduces part of a position — a rebalance queuing
    -50 against a 100-share long — is not an exit intent that can be sized
    from the live position. Collapsing it to "flatten this symbol" fabricates
    a -100 fill, doubles the intended transaction cost, and reports flat when
    the strategy asked for half. Before #1928 widened the terminal population
    to include stranded deferred fills, this order simply never filled; that
    remains the correct outcome."""
    bars = [_bar(15, 30), _bar(15, 31), _bar(15, 32), _bar(15, 33)]
    strategy = _EntryThenExitStrategy(exit_on_bar_index=2, exit_quantity=-50)

    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=ExecutionConfig(fill_mode=FillMode.NEXT_BAR_OPEN),
    )
    engine.run(strategy)

    assert strategy.ctx is not None
    assert [event.tag for event in strategy.order_events] == ["entry"]
    assert strategy.ctx.portfolio.get_position("SPY").quantity == 100


def test_end_of_data_leaves_a_stranded_flip_unfilled():
    """A flip (-150 against a 100-share long) is not a liquidation either —
    it would leave a short. Synthesizing a flatten would silently discard the
    short half of the intent."""
    bars = [_bar(15, 30), _bar(15, 31), _bar(15, 32), _bar(15, 33)]
    strategy = _EntryThenExitStrategy(exit_on_bar_index=2, exit_quantity=-150)

    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=ExecutionConfig(fill_mode=FillMode.NEXT_BAR_OPEN),
    )
    engine.run(strategy)

    assert strategy.ctx is not None
    assert [event.tag for event in strategy.order_events] == ["entry"]
    assert strategy.ctx.portfolio.get_position("SPY").quantity == 100

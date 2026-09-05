"""The warmup → evaluation boundary inside ``BacktestEngine.run``.

A run may begin earlier than the window it is scored on so indicators and
stateful primitives are primed when scoring starts. At the boundary the
engine flushes every consolidated bar that closed before it, asserts the
program is ready, and resets execution state — positions, orders, cash,
the trade ledger, the curve, insights — while indicator memory carries
across. Contract: PRD #1925 "The gate" and review findings F02/F10.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, EvaluationBoundaryError
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.order import FillMode
from app.engine.strategy.base import Strategy
from app.engine.strategy.programs.sma_crossover import SmaCrossoverParams, build_sma_crossover_signal_program

_ET = ZoneInfo("America/New_York")
DAY_ONE = date(2024, 1, 2)
DAY_TWO = date(2024, 1, 3)


def _ny_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=_ET).timestamp() * 1000)


def _bar(day: date, hour: int, minute: int, close: str) -> TradeBar:
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET)
    price = Decimal(close)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=10_000,
    )


def _session(day: date, closes: list[str], *, start_minute: int = 0) -> list[TradeBar]:
    """Consecutive minute bars from 09:30 ET on ``day``."""
    bars = []
    for offset, close in enumerate(closes):
        minutes = 30 + start_minute + offset
        bars.append(_bar(day, 9 + minutes // 60, minutes % 60, close))
    return bars


class _StaticBarReader:
    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        yield from self._bars


class _EnterOnNthBarStrategy(Strategy):
    """Buys the whole portfolio on its ``entry_bar``-th consolidated bar.

    Deliberately holds forever and never resets its own flag except through
    ``on_force_flat`` — the hook the boundary contract relies on.
    """

    def __init__(self, *, period: timedelta, entry_bar: int) -> None:
        super().__init__()
        self._period = period
        self._entry_bar = entry_bar
        self.bars_seen = 0
        self.entered = False
        self.force_flat_calls = 0
        self.trade_log: list = []

    def initialize(self) -> None:
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 1, 3)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, self._period, self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        self.bars_seen += 1
        if not self.entered and self.bars_seen == self._entry_bar:
            self.ctx.set_holdings("SPY", 1.0)
            self.entered = True

    def on_force_flat(self) -> None:
        self.force_flat_calls += 1
        self.entered = False


def _run(bars: list[TradeBar], strategy: Strategy, *, evaluation_start_ms: int | None, fill_mode=FillMode.SIGNAL_BAR_CLOSE):
    engine = BacktestEngine(data_source=_StaticBarReader(bars), execution_config=ExecutionConfig(fill_mode=fill_mode))
    return engine.run(strategy, evaluation_start_ms=evaluation_start_ms)


def test_a_warmup_position_is_flat_at_the_evaluation_start_and_never_becomes_a_scored_trade() -> None:
    warmup = _session(DAY_ONE, ["500", "501", "502", "503"])
    evaluation = _session(DAY_TWO, ["510", "511", "512"])
    strategy = _EnterOnNthBarStrategy(period=timedelta(minutes=1), entry_bar=2)

    result = _run(warmup + evaluation, strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO))

    # The warmup entry filled (the strategy did enter) but nothing about it
    # survives into the evaluation record: no fill, no held shares, and the
    # curve restarts at the configured cash on the first evaluation bar.
    assert strategy.force_flat_calls == 1
    assert result.order_events == []
    assert result.final_equity == Decimal(100_000)
    assert [snapshot.timestamp_ms for snapshot in result.equity_curve] == [bar.end_ms for bar in evaluation]
    assert result.equity_curve[0].equity == Decimal(100_000)
    assert [bar.end_ms for bar in result.bars] == [bar.end_ms for bar in evaluation]
    assert strategy.ctx is not None
    assert all(bar.end_ms >= _ny_midnight_ms(DAY_TWO) for bar in strategy.ctx.consolidated_bars)


def test_the_last_warmup_consolidated_bar_is_flushed_before_the_reset() -> None:
    """A decision taken on the final warmup bar must not become a scored entry.

    The consolidator emits a working bar only when a later input arrives, so
    without an explicit flush the 15:45–16:00 warmup bar would fire on the
    first evaluation minute — after execution state was reset — and its order
    would fill inside the evaluation window (PRD #1925 review F10).
    """
    warmup = _session(DAY_ONE, [str(500 + i) for i in range(30)], start_minute=360)  # 15:30 → 16:00
    evaluation = _session(DAY_TWO, [str(600 + i) for i in range(30)])
    # Two 15-minute warmup bars (15:30 and 15:45); the strategy enters on the second.
    strategy = _EnterOnNthBarStrategy(period=timedelta(minutes=15), entry_bar=2)

    result = _run(warmup + evaluation, strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO), fill_mode=FillMode.NEXT_BAR_OPEN)

    # The second warmup bar was seen during warmup (flushed at the boundary),
    # its deferred fill was cancelled by the reset, and the strategy's own
    # flag was cleared — so evaluation opens flat with no fill at 09:30.
    assert strategy.bars_seen == 4  # 2 warmup + 2 evaluation
    assert strategy.force_flat_calls == 1
    assert result.order_events == []
    assert result.final_equity == Decimal(100_000)


def test_a_daily_cadence_warmup_bar_is_flushed_at_the_boundary_too() -> None:
    warmup = _session(DAY_ONE, [str(500 + i) for i in range(5)])
    evaluation = _session(DAY_TWO, [str(600 + i) for i in range(5)])
    strategy = _EnterOnNthBarStrategy(period=timedelta(days=1), entry_bar=1)

    result = _run(warmup + evaluation, strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO))

    # The day-one bar fired inside warmup (it closed at the boundary), so its
    # entry was discarded; the day-two bar is still working when data ends.
    assert strategy.bars_seen == 1
    assert strategy.force_flat_calls == 1
    assert result.order_events == []


def test_indicator_memory_carries_across_the_boundary_so_scoring_starts_ready() -> None:
    program = build_sma_crossover_signal_program(SmaCrossoverParams(short_window=2, long_window=3, resolution_minutes=1))
    strategy = program.strategy
    warmup = _session(DAY_ONE, ["500", "501", "502", "503", "504"])
    evaluation = _session(DAY_TWO, ["505", "506", "507"])

    _run(warmup + evaluation, strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO))

    boundary = _ny_midnight_ms(DAY_TWO)
    evaluation_traces = [trace for trace in program.session.traces if trace.bar_close_ms >= boundary]
    assert len(evaluation_traces) == len(evaluation)
    # No cold start: the very first scored decision already has ready indicators.
    assert evaluation_traces[0].ready is True


def test_an_unready_program_at_the_boundary_fails_closed() -> None:
    program = build_sma_crossover_signal_program(SmaCrossoverParams(short_window=2, long_window=3, resolution_minutes=1))
    warmup = _session(DAY_ONE, ["500"])  # one sample cannot ready a 3-period SMA
    evaluation = _session(DAY_TWO, ["505", "506", "507"])

    with pytest.raises(EvaluationBoundaryError, match="not ready"):
        _run(warmup + evaluation, program.strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO))


def test_an_evaluation_start_past_the_data_fails_closed() -> None:
    strategy = _EnterOnNthBarStrategy(period=timedelta(minutes=1), entry_bar=99)
    bars = _session(DAY_ONE, ["500", "501"])

    with pytest.raises(EvaluationBoundaryError, match="no bar"):
        _run(bars, strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO))


def test_without_an_evaluation_start_the_run_is_unchanged() -> None:
    bars = _session(DAY_ONE, ["500", "501", "502", "503"])
    strategy = _EnterOnNthBarStrategy(period=timedelta(minutes=1), entry_bar=2)

    result = _run(bars, strategy, evaluation_start_ms=None)

    assert strategy.force_flat_calls == 0
    assert len(result.order_events) == 1
    assert len(result.equity_curve) == len(bars)



class _WarmupNoiseStrategy(Strategy):
    """Enters with a bracket, logs, and emits an insight during warmup — everything the reset must discard."""

    def __init__(self) -> None:
        super().__init__()
        self.bars_seen = 0
        self.trade_log: list = []

    def initialize(self) -> None:
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 1, 3)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        from app.engine.framework.insight import Insight, InsightDirection

        assert self.ctx is not None
        self.bars_seen += 1
        if self.bars_seen == 1:
            self.ctx.portfolio.submit_market_order("SPY", 100, bar.end_ms, tag="entry", take_profit_price=Decimal("505"))
            self.ctx.log("warmup entry")
            self.ctx.emit_insight(Insight(symbol="SPY", direction=InsightDirection.UP, period=timedelta(days=5)))

    def on_force_flat(self) -> None:
        return None


def test_fees_insights_logs_and_brackets_from_warmup_do_not_survive_the_boundary() -> None:
    """Every accumulator the reset names is checked, not only the position (review M10).

    The warmup entry pays a commission, opens a take-profit bracket that the
    600-level evaluation bars would trigger at once, logs a line, and emits a
    five-day insight. None of it may reach the scored record.
    """
    warmup = _session(DAY_ONE, ["500", "501", "502"])
    evaluation = _session(DAY_TWO, ["600", "601", "602"])
    strategy = _WarmupNoiseStrategy()
    engine = BacktestEngine(
        data_source=_StaticBarReader(warmup + evaluation),
        execution_config=ExecutionConfig(fill_mode=FillMode.SIGNAL_BAR_CLOSE, commission_per_order=Decimal("10")),
    )

    result = engine.run(strategy, evaluation_start_ms=_ny_midnight_ms(DAY_TWO))

    assert result.total_fees == Decimal(0)
    assert result.final_equity == Decimal(100_000)
    assert result.order_events == []  # the bracket was cleared, so no TP fill at 600
    assert "warmup entry" not in result.log_lines
    assert result.log_lines[0].startswith("[EVALUATION START]")
    assert result.insights == []
    assert result.insight_summary.get("total_insights", 0) == 0

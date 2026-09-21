"""``retain_bars=False``: a summary run keeps the count, drops the list (#1941).

A Grid Search cell never reads the bars the engine iterated; it read
``len(equity_curve)`` off the response and six statistics. This is the
engine half of that contract: without retention the run must produce the
identical equity curve and result figures — the samples the statistics
consume — and carry the bar count in ``bars_consumed`` instead of a
materialised ``list[TradeBar]``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, EquitySnapshot
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import Strategy

NY = ZoneInfo("America/New_York")


def _minute(date_: date, hour: int, minute: int, *, open_: str, high: str, low: str, close: str) -> TradeBar:
    start = datetime(date_.year, date_.month, date_.day, hour, minute, tzinfo=NY)
    return TradeBar(
        symbol="AAPL",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


class _SyntheticStream:
    """data_source.iter_bars contract: returns a fresh iterator each call."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start_date: date, end_date: date) -> Iterator[TradeBar]:
        return iter(self._bars)


class _EnterExitStrategy(Strategy):
    """Long on the first consolidated bar, flat on the second — a run with trades."""

    def initialize(self) -> None:
        self.set_start_date(2026, 2, 9)
        self.set_end_date(2026, 2, 12)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("AAPL")
        self._symbol = symbol
        self._bars_seen = 0
        self.ctx.register_consolidator(symbol, timedelta(minutes=1440), self._on_daily)

    def _on_daily(self, bar: TradeBar) -> None:
        self._bars_seen += 1
        assert self.ctx is not None
        self.ctx.set_holdings(self._symbol, Decimal("1.0") if self._bars_seen == 1 else Decimal("0"))


def _stream() -> list[TradeBar]:
    # Three days: day-2's first minute fires day-1's daily bar (the entry),
    # day-3's first minute fires day-2's daily bar (the exit) — both fills
    # land in-loop, so nothing depends on the end-of-data flush.
    d1, d2, d3 = date(2026, 2, 9), date(2026, 2, 10), date(2026, 2, 11)
    return [
        _minute(d1, 9, 30, open_="100.0", high="100.2", low="99.9", close="100.1"),
        _minute(d1, 12, 0, open_="100.1", high="100.3", low="100.0", close="100.2"),
        _minute(d1, 15, 59, open_="100.2", high="100.4", low="100.0", close="100.3"),
        _minute(d2, 9, 30, open_="102.0", high="102.5", low="101.8", close="102.2"),
        _minute(d2, 9, 31, open_="102.2", high="102.6", low="102.0", close="102.4"),
        _minute(d2, 15, 59, open_="102.4", high="102.7", low="102.3", close="102.5"),
        _minute(d3, 9, 30, open_="101.0", high="101.4", low="100.8", close="101.2"),
        _minute(d3, 10, 0, open_="101.2", high="101.5", low="101.0", close="101.3"),
    ]


def _run(retain_bars: bool):
    engine = BacktestEngine(
        data_source=_SyntheticStream(_stream()),
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("1"),
            slippage_per_share=Decimal("0"),
        ),
    )
    return engine.run(_EnterExitStrategy(), retain_bars=retain_bars)


def _curve_points(curve: list[EquitySnapshot]) -> list[tuple[int, Decimal, Decimal, Decimal]]:
    return [(s.timestamp_ms, s.equity, s.cash, s.holdings_value) for s in curve]


def test_a_run_without_bar_retention_reports_the_count_and_keeps_every_figure() -> None:
    full = _run(retain_bars=True)
    summary = _run(retain_bars=False)

    # The count replaces the list, and full runs carry it too.
    assert summary.bars == []
    assert summary.bars_consumed == len(full.bars) == full.bars_consumed

    # Everything the statistics consume is produced identically.
    assert _curve_points(summary.equity_curve) == _curve_points(full.equity_curve)
    assert summary.final_equity == full.final_equity
    assert summary.net_profit == full.net_profit
    assert summary.total_fees == full.total_fees
    assert summary.order_events == full.order_events
    assert len(summary.order_events) >= 2  # the entry and the exit actually ran


def test_the_count_describes_the_scored_window_not_the_warmup() -> None:
    """A primed run reads warmup bars it never scores; the count restarts at the
    evaluation boundary together with the curve and the retained bars, so
    ``bars_consumed == len(equity_curve)`` holds in both modes (#1941)."""
    evaluation_start_ms = int(datetime(2026, 2, 10, tzinfo=NY).timestamp() * 1000)

    def _run(retain_bars: bool):
        engine = BacktestEngine(
            data_source=_SyntheticStream(_stream()),
            fill_model=FillModel(
                mode=FillMode.SIGNAL_BAR_CLOSE,
                commission_per_order=Decimal("1"),
                slippage_per_share=Decimal("0"),
            ),
        )
        return engine.run(_EnterExitStrategy(), evaluation_start_ms=evaluation_start_ms, retain_bars=retain_bars)

    full, summary = _run(True), _run(False)
    # Day 1 is warmup (3 bars); days 2-3 are the scored window (5 bars).
    assert summary.bars_consumed == len(full.bars) == len(summary.equity_curve) == 5

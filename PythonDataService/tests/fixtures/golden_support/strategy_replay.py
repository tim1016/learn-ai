"""Shared BacktestEngine replay helper for hand-coded-Strategy golden fixtures.

Runs a ``Strategy`` through ``BacktestEngine`` against a pre-built, in-memory
bar list, pinning the strategy's start/end date to the bar range so callers
don't need session-calendar knowledge. Used identically by a fixture's
generator script (to produce the pinned ``trade_log``) and its validation
test (to reproduce it) — sharing one definition means generation and
validation can never silently drift apart.
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import LoggedTrade, Strategy
from app.services.spec_strategy_runner import InMemoryDataReader
from app.utils.timestamps import ny_datetime


def run_strategy_over_bars(strategy: Strategy, bars: list[TradeBar]) -> list[LoggedTrade]:
    """Run ``strategy`` through BacktestEngine over ``bars`` and return its trade_log.

    Pins ``start_date``/``end_date`` to the bar series' own date range and
    uses zero-fee signal-bar-close fills — the same protocol the repo's other
    strategy-parity synthetic fixtures use (see
    ``app/engine/strategy/spec/tests/_parity_helpers.py::run_strategy``).
    """
    start_date = ny_datetime(min(bar.start_ms for bar in bars)).date()
    end_date = ny_datetime(max(bar.end_ms for bar in bars)).date()
    orig_init = strategy.initialize

    def _patched_init() -> None:
        orig_init()
        strategy.set_start_date(start_date.year, start_date.month, start_date.day)
        strategy.set_end_date(end_date.year, end_date.month, end_date.day)

    strategy.initialize = _patched_init  # type: ignore[method-assign]
    engine = BacktestEngine(
        data_source=InMemoryDataReader(bars),  # type: ignore[arg-type]
        fill_model=FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, commission_per_order=Decimal("0")),
    )
    engine.run(strategy)
    return list(getattr(strategy, "trade_log", []))

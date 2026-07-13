"""Serve consolidated bars from the shared policy-keyed bar store.

Backs ``GET /api/engine/bars``: the UI's way to chart exactly the bytes
a backtest consumed. Reads through the same readers and the same
consolidator the engine itself uses, so for a given
``(roots, symbol, window, session, strategy timeframe)`` the output is
identical to the ``chart_bars`` a live run reported — pinned by the
golden equality test in ``tests/routers/test_engine_bars_endpoint.py``.

Formula: LEAN period-consolidation semantics (floor-rounded bar start,
fire on next-period arrival, end-of-data scan flush).
Reference: ``app/engine/consolidators/trade_bar_consolidator.py``
(LEAN ``TradeBarConsolidator.cs`` port) and the engine's end-of-data
flush at ``app/engine/engine.py`` ("End-of-data consolidator flush").
Canonical implementation: this module composes the canonical reader and
consolidator; it defines no new math.
Validated against: golden equality test vs ``EngineBacktestResponse.chart_bars``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.availability import AvailabilityReport, check_availability
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar

BarsTimespan = Literal["minute", "hour", "day"]


@dataclass(frozen=True, slots=True)
class ConsolidatedBars:
    """Consolidated bars plus the on-disk coverage that produced them."""

    bars: list[TradeBar]
    coverage: AvailabilityReport


def _period_for(timespan: BarsTimespan, multiplier: int) -> timedelta:
    if timespan == "minute":
        return timedelta(minutes=multiplier)
    if timespan == "hour":
        return timedelta(hours=multiplier)
    return timedelta(days=multiplier)


def read_consolidated_bars(
    *,
    roots: list[Path],
    symbol: str,
    start: date,
    end: date,
    session: Literal["regular", "extended"],
    timespan: BarsTimespan,
    multiplier: int,
) -> ConsolidatedBars:
    """Read source bars from the store and consolidate to the strategy timeframe.

    Mirrors the engine run exactly: minute (and hour) timeframes consume
    the minute reader with the requested session filter; day timeframes
    consume the daily reader. Bars stream through one
    ``TradeBarConsolidator`` and the trailing working bar is flushed
    with ``scan(last_input.end_time)`` — the same end-of-data flush the
    engine performs. Missing days are reported in ``coverage``, never
    raised: a display read must not fail because the cache has gaps.
    """
    if timespan == "day":
        source_bars: list[TradeBar] = list(LeanDailyDataReader(roots).iter_bars(symbol, start, end))
        coverage_resolution: Literal["minute", "daily"] = "daily"
    else:
        source_bars = list(LeanMinuteDataReader(roots, session=session).iter_bars(symbol, start, end))
        coverage_resolution = "minute"

    coverage = check_availability(roots, symbol, start, end, resolution=coverage_resolution)

    consolidator = TradeBarConsolidator(_period_for(timespan, multiplier))
    fired: list[TradeBar] = []
    consolidator.on_data_consolidated = fired.append
    for bar in source_bars:
        consolidator.update(bar)
    if source_bars:
        consolidator.scan(source_bars[-1].end_time)

    return ConsolidatedBars(bars=fired, coverage=coverage)

"""Tests for app.engine.consolidators.trade_bar_consolidator.

Consolidator boundary alignment is timestamp-critical. Any drift in the
floor-to-period math produces signals at the wrong bar and breaks LEAN
parity. These tests exercise (a) alignment, (b) OHLCV aggregation, (c)
firing semantics, and (d) the scan() trailing-bar hook.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.consolidators.trade_bar_consolidator import (
    TradeBarConsolidator,
    _floor_to_period,
)
from app.engine.data.trade_bar import TradeBar


def _bar(
    ts: datetime,
    period: timedelta,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int = 1,
    symbol: str = "SPY",
) -> TradeBar:
    return TradeBar(
        symbol=symbol,
        time=ts,
        end_time=ts + period,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def _minute_bar(ts: datetime, price: str, volume: int = 1) -> TradeBar:
    return _bar(ts, timedelta(minutes=1), price, price, price, price, volume)


def test_floor_to_period_aligns_to_wall_clock_grid():
    dt = datetime(2024, 1, 1, 9, 37, tzinfo=UTC)

    assert _floor_to_period(dt, timedelta(minutes=15)) == datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    assert _floor_to_period(dt, timedelta(minutes=5)) == datetime(2024, 1, 1, 9, 35, tzinfo=UTC)
    assert _floor_to_period(dt, timedelta(hours=1)) == datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def test_consolidator_rejects_non_positive_period():
    with pytest.raises(ValueError):
        TradeBarConsolidator(timedelta(seconds=0))


def test_update_does_not_fire_until_period_boundary():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)

    # 14 minute bars inside 09:30–09:45 — should not emit.
    for i in range(14):
        fired = consolidator.update(_minute_bar(start + timedelta(minutes=i), "100"))
        assert fired is None


def test_update_fires_at_first_bar_of_next_period():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    emitted: list[TradeBar] = []
    consolidator.on_data_consolidated = emitted.append

    # Bars 09:30 through 09:44 accumulate into the 09:30 bar.
    for i in range(15):
        consolidator.update(_minute_bar(start + timedelta(minutes=i), str(100 + i)))
    # The 09:45 minute bar arrives — triggers emission of the 09:30 consolidated bar.
    fired = consolidator.update(_minute_bar(start + timedelta(minutes=15), "200"))

    assert fired is not None
    assert fired.time == start
    assert fired.symbol == "SPY"
    assert len(emitted) == 1
    assert emitted[0] is fired


def test_consolidated_bar_ohlc_aggregates_correctly():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)

    prices = ["100.00", "101.50", "99.00", "102.00", "100.50"]
    # One bar per minute, 5 bars, then a trigger bar at 09:45.
    for i, p in enumerate(prices):
        consolidator.update(_bar(start + timedelta(minutes=i), timedelta(minutes=1), p, p, p, p, 10 * (i + 1)))
    fired = consolidator.update(_minute_bar(start + timedelta(minutes=15), "200"))

    assert fired is not None
    assert fired.open == Decimal("100.00")
    assert fired.high == Decimal("102.00")
    assert fired.low == Decimal("99.00")
    assert fired.close == Decimal("100.50")
    # Volume sums 10 + 20 + 30 + 40 + 50 = 150.
    assert fired.volume == 150


def test_consolidated_bar_end_time_is_last_input_end_time():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)

    # Two minute bars at :30 and :44, then :45 triggers emission.
    consolidator.update(_minute_bar(start, "100"))
    last_minute = _minute_bar(start + timedelta(minutes=14), "101")
    consolidator.update(last_minute)
    fired = consolidator.update(_minute_bar(start + timedelta(minutes=15), "102"))

    assert fired is not None
    assert fired.end_time == last_minute.end_time


def test_scan_does_not_emit_if_period_not_reached():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)

    consolidator.update(_minute_bar(start, "100"))
    # 10 minutes into the period — scan should not fire.
    fired = consolidator.scan(start + timedelta(minutes=10))

    assert fired is None


def test_scan_emits_trailing_partial_bar_once_period_elapsed():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    emitted: list[TradeBar] = []
    consolidator.on_data_consolidated = emitted.append

    consolidator.update(_minute_bar(start, "100"))
    fired = consolidator.scan(start + timedelta(minutes=15))

    assert fired is not None
    assert fired.time == start
    assert emitted == [fired]


def test_multiple_consecutive_consolidated_bars_fire_in_order():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    emitted: list[TradeBar] = []
    consolidator.on_data_consolidated = emitted.append

    # Feed minute bars across two and a half 15-minute windows.
    for i in range(35):
        consolidator.update(_minute_bar(start + timedelta(minutes=i), str(100 + i)))

    assert len(emitted) == 2
    assert emitted[0].time == start
    assert emitted[1].time == start + timedelta(minutes=15)

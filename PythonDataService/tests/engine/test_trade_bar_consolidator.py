"""Tests for app.engine.consolidators.trade_bar_consolidator.

Consolidator boundary alignment is timestamp-critical. Any drift in the
floor-to-period math produces signals at the wrong bar and breaks LEAN
parity. These tests exercise (a) alignment, (b) OHLCV aggregation, (c)
firing semantics, and (d) the scan() trailing-bar hook.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.consolidators.trade_bar_consolidator import (
    TradeBarConsolidator,
    _floor_to_period_ms,
)
from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import to_ms_utc

_EASTERN = ZoneInfo("America/New_York")


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
        start_ms=to_ms_utc(ts),
        end_ms=to_ms_utc(ts + period),
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

    assert _floor_to_period_ms(to_ms_utc(dt), timedelta(minutes=15)) == to_ms_utc(datetime(2024, 1, 1, 9, 30, tzinfo=UTC))
    assert _floor_to_period_ms(to_ms_utc(dt), timedelta(minutes=5)) == to_ms_utc(datetime(2024, 1, 1, 9, 35, tzinfo=UTC))
    assert _floor_to_period_ms(to_ms_utc(dt), timedelta(hours=1)) == to_ms_utc(datetime(2024, 1, 1, 9, 0, tzinfo=UTC))


def test_floor_to_period_ms_daily_period_aligns_to_et_midnight_not_utc_midnight():
    """Regression: a bar at 2024-01-05 03:00 UTC is 2024-01-04 22:00 ET —
    still the 2024-01-04 ET trading date. Flooring the raw UTC ms (the
    pre-fix behavior) lands on 2024-01-05 00:00 UTC instead, mislabeling the
    bar with the *next* ET trading date."""
    ts = datetime(2024, 1, 5, 3, 0, tzinfo=UTC)

    floored = _floor_to_period_ms(to_ms_utc(ts), timedelta(days=1))

    assert floored == to_ms_utc(datetime(2024, 1, 4, tzinfo=_EASTERN))


def test_floor_to_period_ms_daily_period_across_dst_spring_forward():
    """2024-03-10 is the US spring-forward transition (ET goes from UTC-5 to
    UTC-4 at 02:00 local). A bar timestamped after the transition must still
    floor to that ET calendar day's midnight, not drift by the hour DST
    changed mid-day."""
    ts = datetime(2024, 3, 10, 8, 30, tzinfo=UTC)  # after the 07:00 UTC transition instant

    floored = _floor_to_period_ms(to_ms_utc(ts), timedelta(days=1))

    assert floored == to_ms_utc(datetime(2024, 3, 10, tzinfo=_EASTERN))


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
    assert fired.start_ms == to_ms_utc(start)
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


def test_consolidated_bar_end_ms_is_last_input_end_ms():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)

    # Two minute bars at :30 and :44, then :45 triggers emission.
    consolidator.update(_minute_bar(start, "100"))
    last_minute = _minute_bar(start + timedelta(minutes=14), "101")
    consolidator.update(last_minute)
    fired = consolidator.update(_minute_bar(start + timedelta(minutes=15), "102"))

    assert fired is not None
    assert fired.end_ms == last_minute.end_ms


def test_scan_does_not_emit_if_period_not_reached():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)

    consolidator.update(_minute_bar(start, "100"))
    # 10 minutes into the period — scan should not fire.
    fired = consolidator.scan(to_ms_utc(start + timedelta(minutes=10)))

    assert fired is None


def test_scan_emits_trailing_partial_bar_once_period_elapsed():
    consolidator = TradeBarConsolidator(timedelta(minutes=15))
    start = datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    emitted: list[TradeBar] = []
    consolidator.on_data_consolidated = emitted.append

    consolidator.update(_minute_bar(start, "100"))
    fired = consolidator.scan(to_ms_utc(start + timedelta(minutes=15)))

    assert fired is not None
    assert fired.start_ms == to_ms_utc(start)
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
    assert emitted[0].start_ms == to_ms_utc(start)
    assert emitted[1].start_ms == to_ms_utc(start + timedelta(minutes=15))

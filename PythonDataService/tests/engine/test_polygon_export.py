"""Tests for app.engine.data.polygon_export pure helpers.

The full export path writes zip files; these tests focus on the pure
helpers that do UTC → Eastern conversion and trading-day bucketing,
which is timestamp-critical per .claude/rules/numerical-rigor.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.polygon_export import (
    _polygon_daily_bar_to_trade_bar,
    group_by_trading_date,
    polygon_bar_to_trade_bar,
)
from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import datetime_at_ms

EASTERN = ZoneInfo("America/New_York")


def testpolygon_bar_to_trade_bar_converts_ms_to_et_start():
    # 2024-04-01 14:30 UTC = 2024-04-01 10:30 ET (DST active).
    ts_ms = int(datetime(2024, 4, 1, 14, 30, tzinfo=UTC).timestamp() * 1000)
    raw = {"timestamp": ts_ms, "open": 515.34, "high": 515.40, "low": 515.30, "close": 515.35, "volume": 1234}

    bar = polygon_bar_to_trade_bar("SPY", raw)

    assert bar.symbol == "SPY"
    assert datetime_at_ms(bar.start_ms, tz=EASTERN) == datetime(2024, 4, 1, 10, 30, tzinfo=EASTERN)
    assert bar.end_ms == bar.start_ms + int(timedelta(minutes=1).total_seconds() * 1000)


def testpolygon_bar_to_trade_bar_prices_are_exact_decimal():
    ts_ms = int(datetime(2024, 4, 1, 14, 30, tzinfo=UTC).timestamp() * 1000)
    raw = {"timestamp": ts_ms, "open": 515.34, "high": 515.40, "low": 515.30, "close": 515.35, "volume": 1234}

    bar = polygon_bar_to_trade_bar("SPY", raw)

    # Str-constructed Decimals avoid float→Decimal round-trip corruption.
    assert bar.open == Decimal("515.34")
    assert bar.close == Decimal("515.35")


def testpolygon_bar_to_trade_bar_null_volume_becomes_zero():
    ts_ms = int(datetime(2024, 4, 1, 14, 30, tzinfo=UTC).timestamp() * 1000)
    raw = {"timestamp": ts_ms, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": None}

    bar = polygon_bar_to_trade_bar("SPY", raw)

    assert bar.volume == 0


def testgroup_by_trading_date_buckets_by_eastern_day():
    # 2024-04-01 03:00 UTC is still 2024-03-31 in ET (23:00).
    bar_wednesday_evening = TradeBar(
        symbol="SPY",
        time=datetime(2024, 4, 1, 3, 0, tzinfo=UTC),
        end_time=datetime(2024, 4, 1, 3, 1, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )
    bar_thursday_morning = TradeBar(
        symbol="SPY",
        time=datetime(2024, 4, 1, 14, 30, tzinfo=UTC),
        end_time=datetime(2024, 4, 1, 14, 31, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )

    buckets = group_by_trading_date([bar_wednesday_evening, bar_thursday_morning])

    assert date(2024, 3, 31) in buckets
    assert date(2024, 4, 1) in buckets


def testgroup_by_trading_date_sorts_bars_chronologically():
    base = datetime(2024, 4, 1, 14, 30, tzinfo=UTC)
    bars = [
        TradeBar(
            symbol="SPY",
            time=base + timedelta(minutes=i),
            end_time=base + timedelta(minutes=i + 1),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=1,
        )
        for i in (3, 0, 2, 1)  # deliberately out of order
    ]

    buckets = group_by_trading_date(bars)

    day_bars = buckets[date(2024, 4, 1)]
    assert [b.start_ms for b in day_bars] == sorted(b.start_ms for b in bars)


def testpolygon_daily_bar_to_trade_bar_spans_exactly_one_day_across_dst_transition():
    """Regression: end_ms must be midnight-to-midnight ET, not start_ms + a
    fixed 86_400_000ms offset — a fixed offset drifts an hour on the day a
    DST transition falls inside the bar (temporal-rigor.md: "DST via the NY
    zone, never a fixed offset"). 2026-03-08 is the day before the US
    spring-forward transition (2026-03-08 02:00 ET -> 03:00 ET)."""
    trading_date = datetime(2026, 3, 8, tzinfo=UTC)
    raw = {
        "timestamp": int(trading_date.timestamp() * 1000),
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1,
    }

    bar = _polygon_daily_bar_to_trade_bar("SPY", raw)

    assert datetime_at_ms(bar.start_ms, tz=EASTERN) == datetime(2026, 3, 8, tzinfo=EASTERN)
    assert datetime_at_ms(bar.end_ms, tz=EASTERN) == datetime(2026, 3, 9, tzinfo=EASTERN)
    # The fixed-offset bug this regression-tests would compute a 23-hour span here.
    assert bar.end_ms - bar.start_ms == 23 * 3_600_000


def testpolygon_daily_bar_to_trade_bar_spans_exactly_24h_off_dst_boundary():
    trading_date = datetime(2026, 6, 15, tzinfo=UTC)
    raw = {
        "timestamp": int(trading_date.timestamp() * 1000),
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1,
    }

    bar = _polygon_daily_bar_to_trade_bar("SPY", raw)

    assert bar.end_ms - bar.start_ms == 24 * 3_600_000

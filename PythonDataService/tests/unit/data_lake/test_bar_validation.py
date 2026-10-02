"""Unit coverage for the lake's vendor minute-bar validator (#2451)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.data_lake.bar_validation import (
    CorruptVendorBarsError,
    assert_publishable_minute_bars,
    assert_publishable_stored_minute_bars,
)
from app.data_lake.polygon_fetcher import PolygonBar

# 2024-05-20 is a Monday; 09:30 ET == 13:30 UTC.
TRADING_DAY = date(2024, 5, 20)
NEXT_DAY_MS_DELTA = 24 * 60 * 60 * 1000
BAR_START_MS = 1716211800000


def _pb(index: int = 0, **overrides: Any) -> PolygonBar:
    fields = {
        "t_ms": BAR_START_MS + index * 60_000,
        "open": 500.0,
        "high": 500.10,
        "low": 499.95,
        "close": 500.05,
        "volume": 1000,
        "vwap": 500.0,
        "n": 10,
    }
    fields.update(overrides)
    return PolygonBar(**fields)


def test_validator_accepts_a_clean_session() -> None:
    assert_publishable_minute_bars([_pb(i) for i in range(390)], symbol="SPY", trading_date=TRADING_DAY)


def test_validator_names_the_offending_bar_with_et_wall_clock() -> None:
    with pytest.raises(CorruptVendorBarsError) as excinfo:
        assert_publishable_minute_bars(
            [_pb(0), _pb(1, high=1.0)], symbol="SPY", trading_date=TRADING_DAY
        )
    message = str(excinfo.value)
    assert "bar[1]" in message
    assert "t_ms=" in message
    assert "(2024-05-20T09:31:00-04:00)" in message  # ET wall-clock, offset included


def test_validator_caps_named_offenders() -> None:
    bars = [_pb(i, high=1.0) for i in range(8)]  # every bar violates
    with pytest.raises(CorruptVendorBarsError, match="8 offending bar"):
        assert_publishable_minute_bars(bars, symbol="SPY", trading_date=TRADING_DAY)
    with pytest.raises(CorruptVendorBarsError) as excinfo:
        assert_publishable_minute_bars(bars, symbol="SPY", trading_date=TRADING_DAY)
    assert "and 3 more" in str(excinfo.value)


def test_validator_rejects_non_positive_and_non_finite_prices() -> None:
    for bad in ({"open": 0.0}, {"close": -1.0}, {"low": float("inf")}, {"high": float("nan")}):
        with pytest.raises(CorruptVendorBarsError, match="not finite and positive"):
            assert_publishable_minute_bars([_pb(0, **bad)], symbol="SPY", trading_date=TRADING_DAY)


def test_validator_rejects_non_increasing_timestamps() -> None:
    with pytest.raises(CorruptVendorBarsError, match="non-increasing timestamp"):
        assert_publishable_minute_bars(
            [_pb(0), _pb(2), _pb(1)], symbol="SPY", trading_date=TRADING_DAY
        )


def test_validator_rejects_next_session_premarket_as_wrong_day() -> None:
    # 2024-05-21 pre-market bar inside a 2024-05-20 capture fetch.
    with pytest.raises(CorruptVendorBarsError, match="outside requested session 2024-05-20"):
        assert_publishable_minute_bars(
            [_pb(0), _pb(t_ms=BAR_START_MS + NEXT_DAY_MS_DELTA)], symbol="SPY", trading_date=TRADING_DAY
        )


def test_validator_accepts_premarket_of_the_requested_session() -> None:
    # 2024-05-20 08:00 ET pre-market bar — same ET date, so publishable.
    assert_publishable_minute_bars(
        [_pb(t_ms=BAR_START_MS - 90 * 60_000), _pb(0)], symbol="SPY", trading_date=TRADING_DAY
    )


def test_stored_bars_validate_through_the_same_contract() -> None:
    """The legacy cache-hit gate reuses the publication contract verbatim:
    a stored duplicate timestamp is the same refusal (#2527 review)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.data_lake.lean_writer import MinuteTradeBar

    et = ZoneInfo("America/New_York")
    start = datetime(2024, 5, 20, 9, 30, tzinfo=et)
    bar = MinuteTradeBar(bar_start_et=start, open=Decimal("100"), high=Decimal("100"),
                         low=Decimal("100"), close=Decimal("100"), volume=10)
    duplicate = MinuteTradeBar(bar_start_et=start, open=Decimal("100"), high=Decimal("100"),
                               low=Decimal("100"), close=Decimal("100"), volume=10)
    with pytest.raises(CorruptVendorBarsError, match="duplicate timestamp"):
        assert_publishable_stored_minute_bars(
            [bar, duplicate], symbol="SPY", trading_date=TRADING_DAY
        )


def test_validator_accepts_fractional_share_volume() -> None:
    """Polygon reports fractional shares since 2026-02-23; that is real volume."""
    assert_publishable_minute_bars(
        [_pb(0, volume=9238.22128)], symbol="SPY", trading_date=TRADING_DAY
    )


def test_validator_rejects_non_finite_volume() -> None:
    for bad in (float("nan"), float("inf")):
        with pytest.raises(CorruptVendorBarsError, match="is not finite"):
            assert_publishable_minute_bars(
                [_pb(0, volume=bad)], symbol="SPY", trading_date=TRADING_DAY
            )

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_lake.derived_daily import (
    aggregate_minute_to_daily,
    build_daily_zip_bytes,
)
from app.data_lake.lean_writer import MinuteTradeBar

ET = ZoneInfo("America/New_York")


def _bar(date_str: str, hour: int, minute: int, close: float) -> MinuteTradeBar:
    y, m, d = (int(x) for x in date_str.split("-"))
    bar_start = datetime(y, m, d, hour, minute, tzinfo=ET)
    return MinuteTradeBar(
        bar_start_et=bar_start,
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.2)),
        low=Decimal(str(close - 0.2)),
        close=Decimal(str(close)),
        volume=1234,
    )


def test_aggregate_minute_to_daily_one_day_one_aggregate():
    bars = [
        _bar("2024-05-20", 9, 30, 500.00),
        _bar("2024-05-20", 9, 31, 500.10),
        _bar("2024-05-20", 9, 32, 500.20),
    ]
    aggs = aggregate_minute_to_daily(bars)
    assert len(aggs) == 1
    a = aggs[0]
    assert a.trading_date.strftime("%Y%m%d") == "20240520"
    # Open = first bar's open; close = last bar's close; high = max of highs.
    assert a.open == Decimal("499.9")
    assert a.close == Decimal("500.2")
    assert a.high == Decimal("500.4")  # 500.20 + 0.2
    assert a.low == Decimal("499.8")  # 500.00 - 0.2
    assert a.volume == 3 * 1234


def test_aggregate_minute_to_daily_two_days_two_aggregates():
    bars = [
        _bar("2024-05-20", 9, 30, 500.00),
        _bar("2024-05-21", 9, 30, 501.00),
    ]
    aggs = aggregate_minute_to_daily(bars)
    assert len(aggs) == 2
    assert aggs[0].trading_date.strftime("%Y%m%d") == "20240520"
    assert aggs[1].trading_date.strftime("%Y%m%d") == "20240521"


def test_build_daily_zip_emits_csv_with_correct_name():
    bars = [_bar("2024-05-20", 9, 30, 500.00)]
    aggs = aggregate_minute_to_daily(bars)
    payload = build_daily_zip_bytes(symbol="SPY", aggregates=aggs)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["spy.csv"]
        csv = zf.read("spy.csv").decode("ascii")
    # One row, comma-separated, deci-cent prices.
    cols = csv.strip().split(",")
    assert cols[0] == "20240520 00:00"
    assert int(cols[4]) == 5_000_000  # close = 500.00 * 10000


def test_build_daily_zip_is_deterministic():
    bars = [_bar("2024-05-20", 9, 30, 500.00)]
    aggs = aggregate_minute_to_daily(bars)
    a = build_daily_zip_bytes("SPY", aggs)
    b = build_daily_zip_bytes("SPY", aggs)
    assert a == b

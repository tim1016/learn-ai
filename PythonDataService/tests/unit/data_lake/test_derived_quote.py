from __future__ import annotations

import io
import zipfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.lean_writer import MinuteTradeBar

ET = ZoneInfo("America/New_York")


def _bar(hour: int, minute: int, close: float) -> MinuteTradeBar:
    bar_start = datetime(2024, 5, 20, hour, minute, tzinfo=ET)
    return MinuteTradeBar(
        bar_start_et=bar_start,
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.2)),
        low=Decimal(str(close - 0.2)),
        close=Decimal(str(close)),
        volume=1234,
    )


def test_quote_zip_named_correctly():
    bars = [_bar(9, 30, 500.00)]
    payload = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["20240520_spy_minute_quote.csv"]


def test_quote_csv_bid_equals_ask_zero_size():
    bars = [_bar(9, 30, 500.00)]
    payload = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        csv = zf.read("20240520_spy_minute_quote.csv").decode("ascii")
    cols = csv.strip().split(",")
    # 11 columns: ms + 5 bid + 5 ask.
    assert len(cols) == 11
    # bid_close == ask_close == trade_close at deci-cent scale.
    assert int(cols[4]) == int(cols[9]) == 5_000_000
    # bid_size = ask_size = 0.
    assert int(cols[5]) == 0
    assert int(cols[10]) == 0


def test_quote_zip_is_deterministic():
    bars = [_bar(9, 30, 500.00), _bar(9, 31, 500.10)]
    a = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    b = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    assert a == b

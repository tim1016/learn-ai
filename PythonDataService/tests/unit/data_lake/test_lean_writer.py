"""Unit tests for app.data_lake.lean_writer.

LEAN minute-trade zip format (see Lean/Common/Data/Market/TradeBar.cs):
  data/equity/usa/minute/<sym_lower>/<yyyymmdd>_trade.zip
    └── <yyyymmdd>_<sym_lower>_minute_trade.csv
        no header; columns: ms_since_midnight_et, open*10000, high*10000,
        low*10000, close*10000, volume
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.data_lake.lean_writer import (
    MinuteTradeBar,
    build_minute_trade_zip_bytes,
    format_volume,
    parse_volume,
    to_deci_cent,
)

ET = ZoneInfo("America/New_York")


def test_to_deci_cent_rounds_half_up():
    assert to_deci_cent(Decimal("499.5")) == 4_995_000
    assert to_deci_cent(Decimal("500.00005")) == 5_000_001
    assert to_deci_cent(Decimal("0")) == 0


def test_to_deci_cent_negative_rejected():
    import pytest

    with pytest.raises(ValueError):
        to_deci_cent(Decimal("-1.0"))


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


def test_build_minute_trade_zip_contains_one_csv_per_symbol_day():
    bars = [_bar(9, 30, 500.00), _bar(9, 31, 500.10)]
    payload = build_minute_trade_zip_bytes(
        symbol="SPY",
        trading_date_yyyymmdd="20240520",
        bars=bars,
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        assert names == ["20240520_spy_minute_trade.csv"]
        csv = zf.read(names[0]).decode("ascii")

    lines = csv.strip().split("\n")
    assert len(lines) == 2
    # First row: 09:30 ET → 34_200_000 ms since midnight ET. 500.00 = 5_000_000.
    cols = lines[0].split(",")
    assert int(cols[0]) == 34_200_000
    assert int(cols[4]) == 5_000_000  # close
    assert int(cols[5]) == 1234  # volume


def test_build_minute_trade_zip_is_deterministic():
    bars = [_bar(9, 30, 500.00), _bar(9, 31, 500.10)]
    a = build_minute_trade_zip_bytes("SPY", "20240520", bars)
    b = build_minute_trade_zip_bytes("SPY", "20240520", bars)
    assert a == b


def test_symbol_is_lowercased_in_csv_name():
    bars = [_bar(9, 30, 500.00)]
    payload = build_minute_trade_zip_bytes("QQQ", "20240520", bars)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["20240520_qqq_minute_trade.csv"]


@pytest.mark.parametrize(
    ("volume", "encoded"),
    [
        (1234, "1234"),
        (Decimal("34307.0"), "34307"),
        (Decimal("1E+3"), "1000"),
        (Decimal("9238.22128"), "9238.22128"),
        (Decimal("5244.999999"), "5244.999999"),
        (Decimal("0.500"), "0.5"),
        (Decimal("50484685.50995"), "50484685.50995"),
    ],
)
def test_format_volume_writes_exact_shares_one_way(volume: Decimal | int, encoded: str):
    """Whole volumes keep the bytes every pre-fractional file has; a
    fractional one keeps every vendor digit; equal values encode equally."""
    assert format_volume(volume) == encoded
    assert parse_volume(encoded) == volume


@pytest.mark.parametrize("volume", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_format_volume_refuses_negative_and_non_finite(volume: Decimal):
    with pytest.raises(ValueError):
        format_volume(volume)


def test_format_volume_refuses_a_float():
    with pytest.raises(TypeError):
        format_volume(9238.22128)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "abc", "-1", "NaN", "Infinity", "1E+3", "+5", " 5", "1.50", "007"])
def test_parse_volume_reads_only_what_format_volume_writes(text: str):
    with pytest.raises(ValueError):
        parse_volume(text)

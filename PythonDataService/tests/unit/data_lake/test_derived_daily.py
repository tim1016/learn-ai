from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data_lake.derived_daily import (
    aggregate_minute_to_daily,
    build_daily_zip_bytes,
    rth_daily_closes,
)
from app.data_lake.lean_writer import MinuteTradeBar
from app.lean_sidecar import trading_calendar
from app.lean_sidecar.trading_calendar import is_regular_session_ms_utc

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


# ── rth_daily_closes ──────────────────────────────────────────────────────

# Two spans: all-EDT with Memorial Day (no session) and July 3rd (13:00 ET close); and
# across the November offset change with Thanksgiving (no session) and Black Friday (13:00 ET close).
_SPANS = (
    ("2024-05-17", "2024-05-20", "2024-05-27", "2024-07-03"),
    ("2025-10-31", "2025-11-03", "2025-11-27", "2025-11-28"),
)
_CLOCK = ((4, 0), (9, 29), (9, 30), (12, 59), (13, 0), (15, 59), (16, 0), (19, 59))


def _span_bars(dates: tuple[str, ...]) -> list[MinuteTradeBar]:
    return [_bar(d, h, m, 100.0 + i) for d in dates for i, (h, m) in enumerate(_CLOCK)]


@pytest.mark.parametrize("dates", _SPANS, ids=("edt-holiday-half-day", "dst-change-thanksgiving-half-day"))
def test_rth_daily_closes_matches_the_per_bar_calendar_rule(dates: tuple[str, ...]) -> None:
    """Identical to filtering every bar through the canonical per-instant rule."""
    bars = _span_bars(dates)
    expected: dict[date, Decimal] = {}
    for bar in bars:
        if is_regular_session_ms_utc(int(bar.bar_start_et.timestamp() * 1000)):
            expected[bar.bar_start_et.date()] = bar.close

    closes = rth_daily_closes(bars)

    assert closes == expected
    assert [d.isoformat() for d in closes] == [dates[0], dates[1], dates[3]]  # the holiday has no close
    assert closes[bars[0].bar_start_et.date()] == Decimal("105.0")  # 15:59 is the last regular-session bar
    assert closes[bars[-1].bar_start_et.date()] == Decimal("103.0")  # 12:59 on the 13:00 early close


def test_rth_daily_closes_builds_one_calendar_schedule_for_the_whole_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for issue #1943: one schedule for the span, not one per minute bar."""
    real = trading_calendar._schedule
    calls: list[tuple[date, date]] = []

    def counting(start: date, end: date) -> pd.DataFrame:
        calls.append((start, end))
        return real(start, end)

    monkeypatch.setattr(trading_calendar, "_schedule", counting)

    rth_daily_closes(_span_bars(_SPANS[1]))

    assert len(calls) == 1


def test_rth_daily_closes_of_nothing_is_empty() -> None:
    assert rth_daily_closes([]) == {}

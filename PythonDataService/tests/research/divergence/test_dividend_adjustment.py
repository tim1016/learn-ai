"""Tests for the TV-style dividend adjustment applied server-side.

Scenarios covered:
  * No dividends in range → passthrough (no price changes).
  * One dividend in range → only bars strictly before ex-date are adjusted.
  * Multiple dividends → adjustments stack on bars predating all of them.
  * Dividend exactly on a bar's date → that bar is NOT adjusted (strict <).
  * Polygon payload conversion ignores malformed rows.

See docs/tv-polygon-validation-gotchas.md §1 for the underlying reason this
adjustment is needed. The magnitudes below are synthetic — we're testing
the arithmetic, not SPY's actual dividend schedule.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from app.research.divergence.ingest import (
    DividendEvent,
    apply_dividend_adjustment,
    dividends_from_polygon_payload,
)


def _ms(d: date, hour: int = 10) -> int:
    """Build a UTC ms timestamp from an ET-ish date. 10:00 UTC = early RTH."""
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp() * 1000)


def _bars(dates: list[date], price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": _ms(d, 14),  # 14:00 UTC = 10:00 ET
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000,
            }
            for d in dates
        ]
    )


def test_no_dividends_passes_through_unchanged():
    df = _bars([date(2026, 3, 10), date(2026, 3, 11)])

    out = apply_dividend_adjustment(df, dividends=())

    pd.testing.assert_frame_equal(out, df)


def test_one_dividend_subtracts_from_bars_before_ex_date():
    df = _bars(
        [
            date(2026, 3, 10),  # before ex-date — adjust
            date(2026, 3, 19),  # before ex-date — adjust
            date(2026, 3, 20),  # ON ex-date — NOT adjusted
            date(2026, 3, 21),  # after ex-date — NOT adjusted
        ],
        price=500.0,
    )
    events = (DividendEvent(ex_date=date(2026, 3, 20), cash_amount=1.64, ticker="SPY"),)

    out = apply_dividend_adjustment(df, events)

    # Before: all bars had open/close 500.0, high 500.5, low 499.5.
    assert out["close"].tolist() == pytest.approx([498.36, 498.36, 500.0, 500.0])
    assert out["open"].tolist() == pytest.approx([498.36, 498.36, 500.0, 500.0])
    assert out["high"].tolist() == pytest.approx([498.86, 498.86, 500.5, 500.5])
    assert out["low"].tolist() == pytest.approx([497.86, 497.86, 499.5, 499.5])
    # Volume must pass through untouched.
    assert out["volume"].tolist() == [1000, 1000, 1000, 1000]


def test_multiple_dividends_stack_on_earliest_bars():
    df = _bars(
        [
            date(2025, 12, 1),  # before BOTH ex-dates
            date(2026, 1, 15),  # after Dec ex-date, before Mar ex-date
            date(2026, 3, 20),  # on Mar ex-date
        ],
        price=600.0,
    )
    events = (
        DividendEvent(ex_date=date(2025, 12, 19), cash_amount=1.98, ticker="SPY"),
        DividendEvent(ex_date=date(2026, 3, 20), cash_amount=1.64, ticker="SPY"),
    )

    out = apply_dividend_adjustment(df, events)

    # Bar 0 pre-dates both dividends → minus 1.98 + 1.64 = 3.62
    # Bar 1 only pre-dates the March one → minus 1.64
    # Bar 2 pre-dates neither (on ex-date, strict <) → unchanged
    assert out["close"].tolist() == pytest.approx([596.38, 598.36, 600.0])


def test_apply_raises_when_timestamp_column_missing():
    df = pd.DataFrame([{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}])
    events = (DividendEvent(ex_date=date(2026, 3, 20), cash_amount=1.0),)

    with pytest.raises(ValueError, match="timestamp"):
        apply_dividend_adjustment(df, events)


def test_polygon_payload_conversion_drops_malformed_rows():
    payload = [
        {"ex_dividend_date": "2026-03-20", "cash_amount": 1.64},
        {"ex_dividend_date": None, "cash_amount": 1.0},  # missing date
        {"ex_dividend_date": "2026-06-19"},  # missing amount
        {"ex_dividend_date": "not-a-date", "cash_amount": 2.0},  # unparseable
        {"ex_dividend_date": "2026-09-18", "cash_amount": 1.72},
    ]

    events = dividends_from_polygon_payload(payload, ticker="SPY")

    assert [e.ex_date for e in events] == [date(2026, 3, 20), date(2026, 9, 18)]
    assert [e.cash_amount for e in events] == [1.64, 1.72]
    assert all(e.ticker == "SPY" for e in events)

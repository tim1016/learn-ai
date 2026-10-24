"""The window and symbol ``GET /api/data-lake/coverage`` and ``/instrument-data`` accept, and the reasons they refuse.

One pure function decides it for both routes, so the reasons are pinned once, here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.data_lake.coverage import CoverageRequestError, CoverageWindow, coverage_window
from app.data_lake.types import MAX_TRADING_RANGE_DAYS


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)


def test_a_symbol_and_an_ordered_window_are_accepted_as_trading_dates() -> None:
    window = coverage_window("SPY", _ms(date(2024, 5, 20)), _ms(date(2024, 5, 24)))

    assert window == CoverageWindow(symbol="SPY", start=date(2024, 5, 20), end=date(2024, 5, 24))


def test_one_day_is_a_window() -> None:
    assert coverage_window("SPY", _ms(date(2024, 5, 20)), _ms(date(2024, 5, 20))).start == date(2024, 5, 20)


@pytest.mark.parametrize(
    ("symbol", "start", "end", "reason"),
    [
        ("", _ms(date(2024, 5, 20)), _ms(date(2024, 5, 24)), "invalid_symbol"),
        ("spy; drop", _ms(date(2024, 5, 20)), _ms(date(2024, 5, 24)), "invalid_symbol"),
        ("SPY", _ms(date(2024, 5, 24)), _ms(date(2024, 5, 20)), "invalid_range"),
        ("SPY", _ms(date(2000, 1, 3)), _ms(date(2030, 1, 3)), "range_too_large"),
        ("SPY", 2**62, 2**62, "invalid_trading_date_ms"),
    ],
)
def test_a_symbol_or_window_the_lake_cannot_read_is_refused_with_a_typed_reason(
    symbol: str, start: int, end: int, reason: str
) -> None:
    with pytest.raises(CoverageRequestError) as refused:
        coverage_window(symbol, start, end)

    assert refused.value.reason == reason
    assert refused.value.detail["reason"] == reason
    assert refused.value.detail["message"] == str(refused.value)


def test_the_longest_window_the_cap_allows_is_accepted_and_one_more_day_is_not() -> None:
    start = date(2020, 1, 6)
    end = date.fromordinal(start.toordinal() + MAX_TRADING_RANGE_DAYS - 1)
    # The cap counts calendar days inclusively, as DataRunSpec does.
    coverage_window("SPY", _ms(start), _ms(end))

    with pytest.raises(CoverageRequestError) as refused:
        coverage_window("SPY", _ms(start), _ms(date.fromordinal(end.toordinal() + 1)))
    assert refused.value.reason == "range_too_large"

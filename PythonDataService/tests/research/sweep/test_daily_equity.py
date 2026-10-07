"""A run's daily equity and the two series drawn from it (hand-computed values, atol=1e-9, rtol=0)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.research.sweep.daily_equity import cumulative_return_series, daily_equity, drawdown_series

_MINUTE_MS = 60_000


def _utc_ms(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp() * 1000)


def test_daily_equity_keeps_each_et_dates_last_point_at_its_session_close() -> None:
    days = [date(2024, 3, 8), date(2024, 3, 11), date(2024, 11, 29)]  # EST, EDT after the DST change, a half-day
    curve = []
    for offset, day in enumerate(days):
        open_ms = session_open_ms_utc(day)
        curve += [
            {"timestamp": open_ms + 15 * _MINUTE_MS, "equity": 100_000.0 + offset, "cash": 0.0},
            {"timestamp": open_ms + 30 * _MINUTE_MS, "equity": 100_500.0 + offset, "cash": 0.0},
        ]

    daily = daily_equity(curve)

    assert daily == [
        (_utc_ms(2024, 3, 8, 21, 0), 100_500.0),  # 16:00 EST
        (_utc_ms(2024, 3, 11, 20, 0), 100_501.0),  # 16:00 EDT
        (_utc_ms(2024, 11, 29, 18, 0), 100_502.0),  # 13:00 EST early close
    ]


def test_daily_equity_refuses_unordered_or_non_session_timestamps() -> None:
    open_ms = session_open_ms_utc(date(2024, 3, 8))

    with pytest.raises(ValueError, match="strictly increase"):
        daily_equity([{"timestamp": open_ms, "equity": 1.0}, {"timestamp": open_ms, "equity": 2.0}])
    with pytest.raises(ValueError, match="int ms UTC"):
        daily_equity([{"timestamp": float(open_ms), "equity": 1.0}])
    with pytest.raises(ValueError, match="not a NYSE session"):
        daily_equity([{"timestamp": _utc_ms(2024, 3, 9, 15, 0), "equity": 1.0}])  # a Saturday


def test_drawdown_series_measures_each_day_against_the_running_peak() -> None:
    daily = [(1, 100.0), (2, 110.0), (3, 99.0), (4, 121.0), (5, 108.9)]

    series = drawdown_series(daily)

    assert [ms for ms, _ in series] == [1, 2, 3, 4, 5]
    # 99/110 - 1 = -0.1; 108.9/121 - 1 = -0.1.
    assert np.allclose([value for _, value in series], [0.0, 0.0, -0.1, 0.0, -0.1], atol=1e-9, rtol=0)
    with pytest.raises(ValueError, match="undefined"):
        drawdown_series([(1, 0.0)])


def test_cumulative_return_series_is_each_days_gain_on_the_starting_capital() -> None:
    series = cumulative_return_series([(1, 101_000.0), (2, 99_990.0), (3, 102_010.0)], 100_000.0)

    assert [ms for ms, _ in series] == [1, 2, 3]
    assert np.allclose([value for _, value in series], [0.01, -0.0001, 0.0201], atol=1e-9, rtol=0)

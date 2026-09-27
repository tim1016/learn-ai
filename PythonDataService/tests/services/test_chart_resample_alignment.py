"""Intraday resample alignment anchors to the session open (#2459).

Regular-session 4-hour bars split the session at its open — 09:30–13:30
and 13:30–close — with the open and close taken from the canonical
trading calendar, never a hardcoded wall-clock. Every other timeframe's
grid is unchanged, and extended-hours bins anchor to the extended
session open (04:00 ET).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.services.chart_service import _resample_bars

_ET = "America/New_York"


def _et_ms(day: date, hour: int, minute: int) -> int:
    return int(pd.Timestamp(day.year, day.month, day.day, hour, minute, tz=_ET).value // 1_000_000)


def _session_minute_frame(session_date: date, *, volume: int = 1) -> pd.DataFrame:
    """One minute bar per scheduled session minute, from the canonical calendar."""
    window = session_window_for_date(session_date)
    return pd.DataFrame(
        {
            "timestamp": range(window.open_ms_utc, window.close_ms_utc, 60_000),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": volume,
        }
    )


def _extended_minute_frame(session_date: date, *, volume: int = 1) -> pd.DataFrame:
    """One minute bar across the platform extended window 04:00–20:00 ET."""
    start = _et_ms(session_date, 4, 0)
    end = _et_ms(session_date, 20, 0)
    return pd.DataFrame(
        {
            "timestamp": range(start, end, 60_000),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": volume,
        }
    )


def _labels_ms(bars: pd.DataFrame) -> list[int]:
    return bars["timestamp"].tolist()


def test_four_hour_regular_session_bars_start_at_the_session_open() -> None:
    """A regular session splits 09:30–13:30 / 13:30–16:00 — 240 then 150 minutes.

    On master the midnight-anchored grid labels the bins 08:30 and 12:30,
    splitting the session 180/210 instead.
    """
    bars = _resample_bars(_session_minute_frame(date(2024, 7, 10)), "4h", "rth")

    assert _labels_ms(bars) == [_et_ms(date(2024, 7, 10), 9, 30), _et_ms(date(2024, 7, 10), 13, 30)]
    assert bars["volume"].tolist() == [240, 150]


def test_four_hour_early_close_comes_from_the_calendar() -> None:
    """July 3, 2024 closes at 13:00 ET: one 4-hour bar, no 13:30 bin.

    The session's own schedule bounds the grid — no hardcoded close or
    open time participates.
    """
    window = session_window_for_date(date(2024, 7, 3))
    minutes = (window.close_ms_utc - window.open_ms_utc) // 60_000
    assert minutes == 210  # the calendar says early close

    bars = _resample_bars(_session_minute_frame(date(2024, 7, 3)), "4h", "rth")

    assert _labels_ms(bars) == [_et_ms(date(2024, 7, 3), 9, 30)]
    assert bars["volume"].tolist() == [210]


def test_one_hour_bars_are_unchanged() -> None:
    """1-hour bins keep the session-open anchor they already had."""
    bars = _resample_bars(_session_minute_frame(date(2024, 7, 10)), "1h", "rth")

    assert _labels_ms(bars) == [_et_ms(date(2024, 7, 10), 9 + i, 30) for i in range(7)]
    assert bars["volume"].tolist() == [60] * 6 + [30]


def test_extended_hours_four_hour_bars_anchor_to_the_extended_open() -> None:
    """ETH bins start at 04:00 ET: four equal 4-hour bins across 04:00–20:00."""
    bars = _resample_bars(_extended_minute_frame(date(2024, 7, 10)), "4h", "eth")

    assert _labels_ms(bars) == [_et_ms(date(2024, 7, 10), 4 + 4 * i, 0) for i in range(4)]
    assert bars["volume"].tolist() == [240] * 4


def test_four_hour_labels_survive_a_dst_boundary_in_wall_clock() -> None:
    """Sessions either side of the 2024-11-03 fall-back keep 09:30/13:30 ET labels.

    A midnight-anchored absolute grid lands an hour off in wall-clock on
    the far side of a DST change; the per-session calendar anchor cannot.
    """
    frame = pd.concat(
        [_session_minute_frame(date(2024, 11, 1)), _session_minute_frame(date(2024, 11, 4))],
        ignore_index=True,
    )

    bars = _resample_bars(frame, "4h", "rth")

    assert _labels_ms(bars) == [
        _et_ms(date(2024, 11, 1), 9, 30),
        _et_ms(date(2024, 11, 1), 13, 30),
        _et_ms(date(2024, 11, 4), 9, 30),
        _et_ms(date(2024, 11, 4), 13, 30),
    ]

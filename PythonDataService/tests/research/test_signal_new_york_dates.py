"""The Signal study groups its bars by the New York date, not the UTC date (#3522; ADR 0022).

Under extended hours (04:00-20:00 ET) the bars of 19:00-19:59 ET are the next UTC date in standard time. Bucketed
by UTC date they left their session: a regime day lost its evening, a Friday's evening became a Saturday "day", and a
month's last session handed its evening bars to the next month's walk-forward fold. These tests pin the New York
date for the regime labels, the walk-forward months and the per-bar date labels, on bars shaped like the vendor's
extended tape (the calendar's own open and close, the pre-market head and the after-hours tail).

Regular-hours bars sit on one UTC date in both clock regimes, so the first test proves the change cannot move them:
the New York date equals the UTC date at every regular minute the calendar schedules.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.data_lake.capture_finality import AFTER_HOURS_TAIL_MS, PRE_MARKET_HEAD_MS
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_close_ms_utc,
    session_open_ms_utc,
    session_windows_ms_utc,
)
from app.research.signal.backtest import run_backtest
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine
from app.research.signal.regime import compute_bar_regime_gate, compute_daily_regime_labels
from app.research.signal.session_dates import ny_session_dates
from app.research.signal.walk_forward import run_walk_forward

ET = ZoneInfo("America/New_York")
MINUTE_MS = 60_000
BAR_MINUTES = 15


def _ny_date(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, ET).date()


def _utc_date(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).date()


def _instant(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).timestamp() * 1000)


def _bar(start_ms: int, previous_close: float, close: float) -> dict:
    return {
        "timestamp": start_ms,
        "open": previous_close,
        "high": max(previous_close, close) + 0.01,
        "low": min(previous_close, close) - 0.01,
        "close": close,
        "volume": 1_000.0,
    }


def _extended_session(day: date, previous_close: float, close_of: Callable[[int, float, int], float]) -> list[dict]:
    """One session's bars as the vendor serves them: pre-market head to after-hours tail, ``BAR_MINUTES`` apart.

    ``close_of(index, previous_close, start_ms)`` gives each bar's close, so a test shapes the volatility of the
    daytime and of the evening apart.
    """
    first = session_open_ms_utc(day) - PRE_MARKET_HEAD_MS
    last = session_close_ms_utc(day) + AFTER_HOURS_TAIL_MS
    bars: list[dict] = []
    close = previous_close
    for index, start in enumerate(range(first, last, BAR_MINUTES * MINUTE_MS)):
        new_close = close_of(index, close, start)
        bars.append(_bar(start, close, new_close))
        close = new_close
    return bars


def _random_walk(sigma: float, rng: np.random.Generator) -> Callable[[int, float, int], float]:
    return lambda _index, close, _start: max(close * (1.0 + float(rng.normal(0.0, sigma))), 1.0)


def _calm_day_wild_evening(day: date, rng: np.random.Generator) -> Callable[[int, float, int], float]:
    """Small moves until 19:00 ET, then 1% swings up and down: the evening alone carries the session's volatility."""
    evening = _evening_start(day)

    def close_of(index: int, close: float, start: int) -> float:
        if start >= evening:
            return close * (1.01 if index % 2 else 0.99)
        return close * (1.0 + float(rng.normal(0.0, 0.0001)))

    return close_of


def _evening_start(day: date) -> int:
    return _instant(day, 19, 0)


# ---------------------------------------------------------------------------
# The date itself
# ---------------------------------------------------------------------------


def test_regular_hours_bars_have_the_same_new_york_and_utc_date_so_their_numbers_cannot_move():
    # Three years: both clock regimes, the early closes and every holiday in them.
    windows = session_windows_ms_utc(date(2023, 1, 3), date(2025, 12, 31))
    assert len(windows) > 700 and any(w.close_ms_utc - w.open_ms_utc < 6 * 3_600_000 for w in windows), "early closes are in"
    instants = [ms for w in windows for ms in (w.open_ms_utc, w.close_ms_utc - MINUTE_MS)]

    new_york = ny_session_dates(pd.Series(instants, dtype="int64")).tolist()

    assert new_york == [_utc_date(ms) for ms in instants]
    assert new_york == [w.session_date for w in windows for _ in (0, 1)]


@pytest.mark.parametrize(
    ("day", "clock", "utc_date_of_it"),
    [
        pytest.param(date(2025, 1, 31), (19, 0), date(2025, 2, 1), id="standard_time_19_00_is_the_next_utc_date"),
        pytest.param(date(2025, 1, 31), (19, 59), date(2025, 2, 1), id="standard_time_19_59_is_the_next_utc_date"),
        pytest.param(date(2024, 12, 31), (19, 30), date(2025, 1, 1), id="across_the_year_end"),
        pytest.param(date(2024, 6, 28), (19, 59), date(2024, 6, 28), id="daylight_time_stays_on_one_utc_date"),
        pytest.param(date(2025, 3, 7), (19, 30), date(2025, 3, 8), id="the_friday_before_the_clock_change"),
        pytest.param(date(2025, 3, 10), (19, 30), date(2025, 3, 10), id="the_monday_after_the_clock_change"),
    ],
)
def test_an_evening_bar_keeps_its_sessions_new_york_date_in_either_clock_regime(day, clock, utc_date_of_it):
    instant = _instant(day, *clock)

    assert _utc_date(instant) == utc_date_of_it, "the case is what it says it is"
    assert ny_session_dates(pd.Series([instant], dtype="int64")).iloc[0] == day


def test_the_date_series_keeps_the_index_it_was_given():
    stamps = pd.Series([_instant(date(2025, 1, 31), 19, 30), _instant(date(2025, 2, 3), 4, 0)], index=[7, 3], dtype="int64")

    assert ny_session_dates(stamps).to_dict() == {7: date(2025, 1, 31), 3: date(2025, 2, 3)}


# ---------------------------------------------------------------------------
# The Signal study's groupings
# ---------------------------------------------------------------------------


def test_a_sessions_evening_bars_count_in_that_sessions_regime_not_in_a_day_of_their_own():
    # Standard time, a month's last session (Fri Jan 31): five sessions, the last calm by day and wild by evening.
    sessions = expected_sessions(date(2025, 1, 27), date(2025, 1, 31))
    assert len(sessions) == 5 and sessions[-1] == date(2025, 1, 31)
    rng = np.random.default_rng(5)
    bars: list[dict] = []
    close = 150.0
    for day, sigma in zip(sessions[:-1], (0.0008, 0.0010, 0.0012, 0.0014), strict=True):
        session = _extended_session(day, close, _random_walk(sigma, rng))
        bars += session
        close = session[-1]["close"]
    bars += _extended_session(sessions[-1], close, _calm_day_wild_evening(sessions[-1], rng))

    daily = compute_daily_regime_labels(bars)
    gate = compute_bar_regime_gate(bars, pd.Series([b["timestamp"] for b in bars]))

    assert daily["date"].tolist() == sessions, "one regime day per session: no day made of one evening's bars"
    last = daily.set_index("date").loc[sessions[-1]]
    assert last["vol_regime"] == "High Vol", "the evening's moves are part of that session's volatility"
    last_session_gate = [g for g, b in zip(gate.tolist(), bars, strict=True) if _ny_date(b["timestamp"]) == sessions[-1]]
    assert set(last_session_gate) == {0.0}, "daytime and evening bars of one session carry one gate"


@pytest.fixture(scope="module")
def four_standard_time_months() -> tuple[list[date], list[dict]]:
    """Nov 4 2024 to Feb 28 2025, every session on the vendor's extended shape: Jan 31 is January's last session."""
    sessions = expected_sessions(date(2024, 11, 4), date(2025, 2, 28))
    rng = np.random.default_rng(11)
    bars: list[dict] = []
    close = 150.0
    for day in sessions:
        session = _extended_session(day, close, _random_walk(0.0012, rng))
        bars += session
        close = session[-1]["close"]
    return sessions, bars


def test_a_walk_forward_month_ends_and_starts_on_session_dates_and_keeps_every_evening_bar(four_standard_time_months):
    _, bars = four_standard_time_months
    in_february = [b for b in bars if _ny_date(b["timestamp"]).month == 2]
    before_february = [b for b in bars if _ny_date(b["timestamp"]) <= date(2025, 1, 31)]
    jan_31_evening = [b for b in bars if _ny_date(b["timestamp"]) == date(2025, 1, 31) and b["timestamp"] >= _evening_start(date(2025, 1, 31))]
    assert jan_31_evening and all(_utc_date(b["timestamp"]) == date(2025, 2, 1) for b in jan_31_evening), "the case: UTC says February"

    result = run_walk_forward(bars, "momentum_5m", SignalConfig(min_bars_for_signal=100))

    assert len(result.windows) == 1
    window = result.windows[0]
    # February starts on its first session (Mon Feb 3), not on the Saturday the UTC date gave Friday's evening.
    assert (window.train_end, window.test_start, window.test_end) == ("2025-01-31", "2025-02-03", "2025-02-28")
    assert window.test_bars == len(in_february), "February's bars are February's sessions', evenings included"
    assert window.train_bars == len(before_february), "January's last evening is still January's"
    assert {d[:7] for d in window.oos_dates} == {"2025-02"} and "2025-02-01" not in window.oos_dates


def test_the_whole_report_counts_one_regime_day_per_session_and_one_fold_for_the_months_it_has(four_standard_time_months):
    sessions, bars = four_standard_time_months
    config = SignalConfig(min_bars_for_signal=100)

    report = run_signal_engine("SPY", "momentum_5m", bars, sessions[0].isoformat(), sessions[-1].isoformat(), config)

    assert report.error is None, report.error
    vol_days = sum(report.regime_coverage.get(label, 0) for label in ("Low Vol", "Normal Vol", "High Vol"))
    trend_days = sum(report.regime_coverage.get(label, 0) for label in ("Sideways", "Trending Up", "Trending Down"))
    assert (vol_days, trend_days) == (len(sessions), len(sessions)), "no Saturday, no holiday, no half of a session"
    assert report.walk_forward is not None and [w.test_start for w in report.walk_forward.windows] == ["2025-02-03"]
    labelled = {day for backtest in report.backtest_grid for day in backtest.dates}
    assert labelled and labelled <= {day.isoformat() for day in sessions}, "a per-bar date label is a session's date"


def test_a_bars_date_label_in_a_backtest_is_its_new_york_session_date():
    stamps = pd.Series([_instant(date(2025, 1, 31), 15, 0), _instant(date(2025, 1, 31), 19, 15), _instant(date(2025, 1, 31), 19, 45), _instant(date(2025, 2, 3), 4, 0)])
    signal = pd.Series([1.0, -1.0, 1.0, -1.0])
    returns = pd.Series([0.001, -0.002, 0.003, -0.001])

    result = run_backtest(signal, returns, cost_bps=0.0, timestamps=stamps, include_series=True)

    assert result.dates == ["2025-01-31", "2025-01-31", "2025-01-31", "2025-02-03"]

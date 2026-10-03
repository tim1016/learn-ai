"""A seal's warmup lookback holds the history its own periods need (#2841).

``SignalProgramContract.resolved_warmup_lookback_days`` turns the longest
series a deploy builds into calendar days of lookback. The rule is a fixed
worst-case count, so a seal reads the same on any day. These tests hold it to
the canonical NYSE calendar: on every minute of three years of possible
starts, the window those days open holds at least the decision bars the
longest series needs.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

import numpy as np
import pytest

from app.engine.strategy.params import decision_timeframe_ms_for
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.marketdata.feed import warmup_window_start_ms
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import ny_datetime
from tests._helpers.signal_program import LONG_PERIODS, OFF_VALIDATED_PERIODS

_MINUTE_MS = 60_000

# Every start in these three years is swept. They hold each stretch the
# conversion must survive: three Thanksgiving weeks, Christmas on a Wednesday,
# a Thursday and a Friday (the Friday leaves an early close, a holiday and a
# weekend in a row), the 2025-01-09 day of mourning a week after New Year,
# every three-day weekend, and both clock changes.
_SWEEP_FIRST_DATE = date(2024, 7, 1)
_SWEEP_LAST_DATE = date(2027, 6, 30)
# Sessions before the sweep that its earliest starts look back on.
_LONGEST_LOOKBACK_DAYS = 60

_BEYOND_THE_DEFAULT_LOOKBACK = [
    *[pytest.param(key, periods, id=f"{key}-long") for key, periods in sorted(LONG_PERIODS.items())],
    # Default periods on hourly bars: here the cadence, not a period, is what
    # the default lookback cannot hold.
    pytest.param("sma_crossover", {"resolution_minutes": 60}, id="sma_crossover-hourly"),
]
_POINTS = [
    *[pytest.param(key, {}, id=f"{key}-validated") for key in sorted(OFF_VALIDATED_PERIODS)],
    *[pytest.param(key, periods, id=f"{key}-off_validated") for key, periods in sorted(OFF_VALIDATED_PERIODS.items())],
    *_BEYOND_THE_DEFAULT_LOOKBACK,
]


@cache
def _starts_ms() -> np.ndarray:
    """Every minute of the sweep, as ``int64 ms UTC``: each instant a run could start at."""
    return np.arange(
        et_midnight_ms(_SWEEP_FIRST_DATE), et_midnight_ms(_SWEEP_LAST_DATE), _MINUTE_MS, dtype=np.int64
    )


@cache
def _regular_hours_decision_bars(decision_ms: int) -> tuple[np.ndarray, np.ndarray]:
    """Every regular-hours decision bar the sweep can look back on: (first minute, close), ascending.

    One bar closes on each ``decision_ms`` boundary of the New York wall clock
    inside a session, which is where the runner's consolidator closes it. A
    bar that straddles the open holds only that session's minutes.
    """
    first_minutes: list[int] = []
    closes: list[int] = []
    first_session = _SWEEP_FIRST_DATE - timedelta(days=_LONGEST_LOOKBACK_DAYS)
    for window in session_windows_ms_utc(first_session, _SWEEP_LAST_DATE):
        offset = ny_datetime(window.open_ms_utc).utcoffset()
        assert offset is not None
        wall_clock_ms = int(offset.total_seconds() * 1000)
        close_ms = ((window.open_ms_utc + wall_clock_ms) // decision_ms + 1) * decision_ms - wall_clock_ms
        while close_ms <= window.close_ms_utc:
            first_minutes.append(max(close_ms - decision_ms, window.open_ms_utc))
            closes.append(close_ms)
            close_ms += decision_ms
    return np.array(first_minutes, dtype=np.int64), np.array(closes, dtype=np.int64)


def _fewest_whole_decision_bars(lookback_days: int, decision_ms: int) -> int:
    """The fewest whole regular-hours decision bars a ``lookback_days`` window holds, over every start in the sweep.

    A bar counts when the window holds all of its minutes: its first minute
    is not before the window's start and it has closed by the start instant.
    """
    assert lookback_days <= _LONGEST_LOOKBACK_DAYS
    first_minutes, closes = _regular_hours_decision_bars(decision_ms)
    starts = _starts_ms()
    # The window the feed's own definition opens: a fixed span before the start.
    first_start = int(starts[0])
    window_ms = first_start - warmup_window_start_ms(lookback_days, now_ms=first_start)
    closed_by_start = np.searchsorted(closes, starts, side="right")
    cut_or_before_window = np.searchsorted(first_minutes, starts - window_ms, side="left")
    return int((closed_by_start - cut_or_before_window).min())


def _longest_warmup(program_key: str, overrides: dict[str, int]) -> tuple[int, int, int, int]:
    """``(bars the longest series needs, decision bar ms, resolved lookback days, default lookback days)``."""
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    assert contract is not None
    params = registration.param_schema(**{**contract.validated_settings, **overrides})
    bars = max(series.warmup_bars for series in contract.resolved_signals(params))
    decision_ms = decision_timeframe_ms_for(params, qualified_ms=contract.decision_timeframe_ms)
    return bars, decision_ms, contract.resolved_warmup_lookback_days(params), contract.warmup_lookback_days


@pytest.mark.parametrize(("program_key", "overrides"), _POINTS)
def test_the_resolved_lookback_holds_the_longest_series_warmup_on_every_start(
    program_key: str, overrides: dict[str, int]
) -> None:
    """Whenever the bot starts, the history its seal names warms its longest series.

    Counted in regular hours only, the fewest bars a session holds: a run
    that also decides in extended hours sees more.
    """
    bars, decision_ms, lookback_days, _default_days = _longest_warmup(program_key, overrides)

    assert _fewest_whole_decision_bars(lookback_days, decision_ms) >= bars


@pytest.mark.parametrize(("program_key", "overrides"), _BEYOND_THE_DEFAULT_LOOKBACK)
def test_the_default_lookback_cannot_warm_a_deploy_beyond_it(program_key: str, overrides: dict[str, int]) -> None:
    """What #2841 repaired: on some start the default lookback leaves these deploys unready.

    It also keeps the test above honest, since a point the default already
    warms would pass it without the lookback following anything.
    """
    bars, decision_ms, lookback_days, default_days = _longest_warmup(program_key, overrides)

    assert _fewest_whole_decision_bars(default_days, decision_ms) < bars
    assert lookback_days > default_days


def test_a_decision_bar_longer_than_a_session_keeps_the_default_lookback() -> None:
    """No whole daily bar closes inside regular hours, so no lookback is promised to warm one."""
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    contract = registration.signal_program_contract
    assert contract is not None
    daily = registration.param_schema(resolution_minutes=1440)

    assert contract.resolved_warmup_lookback_days(daily) == contract.warmup_lookback_days
